#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           SPECTER — DASHBOARD SERVER  (dashboard_server.py)                 ║
║                                                                              ║
║  Flask + Socket.IO dashboard backend. Runs on Pi 1 (Master).               ║
║  • Subscribes to all MQTT topics                                             ║
║  • Pushes live updates to dashboard.html via WebSocket                      ║
║  • Serves dashboard.html on port 5000                                       ║
║  • Exposes /api/state and /api/status JSON endpoints                        ║
║  • <250ms DF bearing update latency                                         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, Response
from flask_socketio import SocketIO, emit

CONFIG_PATH = Path("/etc/specter/specter.json")
DASHBOARD_DIR = Path(__file__).parent
VERSION = "1.2.0"
START_TIME = time.time()

# Same-origin CORS and no anonymous MQTT stop a page on another origin or
# host from driving this dashboard, but neither one authenticates a person
# who is already on the LAN and points a browser straight at port 5000 -
# that person could read /api/state (every casualty/patient reading that
# has ever crossed MQTT) and invoke trigger_rx with nothing else required.
# HTTP Basic Auth closes that gap. /api/status is the one deliberate
# exception - a bare liveness probe (service/version/uptime/ok, no patient
# or system state) that scripts/health_check.sh polls unauthenticated, the
# same way a load balancer health check normally would.
DASHBOARD_AUTH_DEFAULT_USERNAME = "operator"
DASHBOARD_AUTH_DEFAULT_PASSWORD = "specter-change-me"

# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "dashboard"
# account: broad READ across shtf/# to drive the UI, zero WRITE - a
# leaked dashboard credential can only observe, never forge a command.
# Fallback values below are used only when specter.json has no
# mqtt.services.dashboard entry (e.g. running outside a real install).
MQTT_SERVICE_KEY      = "dashboard"
MQTT_DEFAULT_USERNAME = "specter-dashboard"
MQTT_DEFAULT_PASSWORD = "specter-change-me"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-dash] %(message)s",
)
log = logging.getLogger("specter.dashboard")

# ─── Load config ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {
            "mqtt": {"broker": "192.168.1.1", "port": 1883},
            "dashboard": {"host": "0.0.0.0", "port": 5000},
        }

cfg = load_config()


def _dashboard_auth_credentials() -> tuple:
    """Operator login for the dashboard/RESUS/WARD web UI - a separate
    credential from any MQTT account, so rotating one doesn't force
    rotating the other. Read from specter.json's dashboard.auth block
    (written by the installer); falls back to the documented default with
    a loud startup warning, same pattern as the MQTT default-password
    check in deploy/install_specter.py."""
    auth_cfg = cfg.get("dashboard", {}).get("auth", {})
    username = auth_cfg.get("username", DASHBOARD_AUTH_DEFAULT_USERNAME)
    password = auth_cfg.get("password", DASHBOARD_AUTH_DEFAULT_PASSWORD)
    if password == DASHBOARD_AUTH_DEFAULT_PASSWORD:
        log.warning(
            "Dashboard is using the DEFAULT operator password - this is "
            "public (it's in the git repo), so it authenticates no one. "
            "Set dashboard.auth.password in specter.json before relying "
            "on this for anything but a bench bring-up."
        )
    return username, password


DASHBOARD_AUTH_USERNAME, DASHBOARD_AUTH_PASSWORD = _dashboard_auth_credentials()


def _check_auth(username: str, password: str) -> bool:
    # compare_digest avoids leaking password length/prefix through
    # response-time differences - a plain == here would be a real (if
    # minor) timing side-channel on a LAN.
    return (
        hmac.compare_digest(username, DASHBOARD_AUTH_USERNAME)
        and hmac.compare_digest(password, DASHBOARD_AUTH_PASSWORD)
    )


def _unauthorized() -> Response:
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="SPECTER Dashboard"'},
    )


# ─── Shared state ─────────────────────────────────────────────────────────────

STATE: dict = {
    "pis": {},
    "df": {"bearing": None, "confidence": 0, "updated": 0},
    "radar": {"contacts": {}, "updated": 0},
    "rx": {"recording": False, "last_file": "", "last_trigger": "", "capture_count": 0},
    "tx": {"status": "idle", "frequency": None},
    "sdr": {"devices": {}},
    "thermal": {"cpu_temp_c": 0, "throttle": {}},
    "ward": {"episodes": [], "updated": 0},
    "mesh": {"status": "unknown", "updated": 0, "messages": []},
    "alarms": [],
    "system": {"uptime": 0, "version": VERSION},
    "mqtt_connected": False,
}
STATE_LOCK = threading.Lock()

# Set by main() once the real DashboardMQTT instance exists - the
# ward_command Socket.IO handler needs to reach it to publish. None in
# tests/anything that imports this module without running main().
_dashboard_mqtt = None

# ─── Flask / SocketIO ─────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=str(DASHBOARD_DIR))
# A hardcoded secret here would be the same value in every SPECTER install
# (it's in the git repo) - functionally no secret at all. Prefer an
# operator-set value from specter.json (dashboard.secret_key, written once
# by the installer) so it's install-specific and stable across restarts;
# fall back to a random one generated at each startup, which is still a
# real per-process secret, it just means any Flask session existing at
# restart time is invalidated (there is no session-based feature relying
# on that yet - see docs/MANUAL.md Part 7.4 on dashboard/RESUS auth gaps).
app.config["SECRET_KEY"] = cfg.get("dashboard", {}).get("secret_key") or secrets.token_hex(32)
# cors_allowed_origins="*" let ANY origin's page drive this dashboard's
# WebSocket (including trigger_rx) via a browser that merely has LAN
# access to it. Default to flask-socketio's same-origin-only behavior
# (cors_allowed_origins=None) unless the installer explicitly configures a
# list of trusted origins (e.g. a separate kiosk host) in specter.json.
socketio = SocketIO(
    app,
    cors_allowed_origins=cfg.get("dashboard", {}).get("cors_allowed_origins"),
    # eventlet itself is in maintenance-only mode upstream (its own import
    # emits EventletDeprecationWarning - visible in this project's own test
    # output). Not an immediate break, but flask-socketio's other
    # production async_mode options (gevent, or threading for lower
    # concurrency) should replace it before eventlet stops receiving
    # security fixes - tracked in docs/MANUAL.md Part 7.2, not silently
    # left as a warning nobody owns.
    async_mode="eventlet",
)


@app.before_request
def _require_auth():
    # /api/status is the one deliberate exception - see the constant block
    # above for why. Everything else (the dashboard/RESUS/WARD pages,
    # /api/state, and the vendored JS assets under /dashboard/vendor/)
    # requires the operator credential.
    if request.path == "/api/status":
        return None
    auth = request.authorization
    if not auth or not _check_auth(auth.username or "", auth.password or ""):
        return _unauthorized()
    return None


@app.route("/")
def index():
    html_path = DASHBOARD_DIR / "dashboard.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER Dashboard</h1><p>dashboard.html not found.</p>", 404


@app.route("/resus")
def resus():
    html_path = DASHBOARD_DIR / "resus.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER RESUS</h1><p>resus.html not found.</p>", 404


@app.route("/ward")
def ward():
    html_path = DASHBOARD_DIR / "ward.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER WARD</h1><p>ward.html not found.</p>", 404


@app.route("/api/state")
def api_state():
    with STATE_LOCK:
        return jsonify(dict(STATE))


@app.route("/api/status")
def api_status():
    return jsonify({
        "service": "specter-dashboard",
        "version": VERSION,
        "uptime":  int(time.time() - START_TIME),
        "ok":      True,
    })


@socketio.on("connect")
def on_connect():
    # The Socket.IO handshake is a normal HTTP request before it upgrades,
    # so the same Basic Auth header check applies here - without this, a
    # client could skip the (now-gated) HTTP page entirely and connect the
    # WebSocket directly to read live state and call trigger_rx.
    auth = request.authorization
    if not auth or not _check_auth(auth.username or "", auth.password or ""):
        log.warning("Rejected unauthenticated WebSocket connect attempt")
        return False
    log.info("WebSocket client connected")
    with STATE_LOCK:
        emit("state", dict(STATE))
    return None


@socketio.on("ward_command")
def on_ward_command(data):
    """
    Real write-back path for WARD mode (unlike RESUS/trigger_rx, this
    reaches the actual ward service over MQTT, not a local file/demo
    state) - see the ACL note on the "dashboard" service in
    deploy/install_specter.py's MQTT_SERVICES for why this credential is
    allowed to write exactly shtf/ward/command/# and nothing else.
    Already behind this connection's Basic-Auth-gated on_connect check;
    publish_ward_command() adds its own allowlist on top of the broker
    ACL as defense in depth.
    """
    if not isinstance(data, dict) or "cmd" not in data:
        log.warning("Malformed ward_command payload: %r", data)
        return
    cmd = data["cmd"]
    payload = {k: v for k, v in data.items() if k != "cmd"}
    if _dashboard_mqtt is None:
        log.warning("ward_command %s dropped - MQTT not initialized", cmd)
        return
    _dashboard_mqtt.publish_ward_command(cmd, payload)


@socketio.on("trigger_rx")
def on_trigger_rx():
    """Operator presses RX capture button on dashboard."""
    trigger = Path("/run/specter/sdr_trigger")
    try:
        trigger.touch()
        log.info("RX trigger sent via dashboard")
    except Exception as e:
        log.warning("Trigger file write failed: %s", e)


# ─── MQTT subscriber ──────────────────────────────────────────────────────────

class DashboardMQTT:
    TOPIC_MAP = {
        "shtf/pi/+/status":     "_on_pi_status",
        "shtf/df/bearing":      "_on_df_bearing",
        "shtf/radar/contacts/+":"_on_radar_contact",
        "shtf/rx/status":       "_on_rx_status",
        "shtf/rx/event":        "_on_rx_event",
        "shtf/rx/recording":    "_on_rx_recording",
        "shtf/tx/status":       "_on_tx_status",
        "shtf/sdr/status":      "_on_sdr_status",
        "shtf/system/thermal":  "_on_thermal",
        "shtf/system/alarm":    "_on_alarm",
        "shtf/system/state":    "_on_system_state",
        "shtf/ward/episode":    "_on_ward_episode",
        "shtf/ward/alert":      "_on_ward_alert",
        "shtf/mesh/status":     "_on_mesh_status",
        "shtf/mesh/inbound":    "_on_mesh_inbound",
    }

    def __init__(self, broker: str, port: int,
                 username: str = MQTT_DEFAULT_USERNAME,
                 password: str = MQTT_DEFAULT_PASSWORD):
        self.broker   = broker
        self.port     = port
        self.username = username
        self.password = password
        self._client = None

    def _parse(self, payload: bytes) -> dict | str:
        try:
            return json.loads(payload.decode())
        except Exception:
            return payload.decode(errors="replace")

    def _push(self, event: str, data: dict) -> None:
        socketio.emit(event, data)

    def _on_pi_status(self, topic: str, data):
        pi_name = topic.split("/")[2]
        last_seen = time.time()
        with STATE_LOCK:
            STATE["pis"][pi_name] = {"last_seen": last_seen, "data": data}
        # last_seen must travel with the push - it's the wrapper-level
        # receive time the server stamps, not anything in `data` itself,
        # and the client needs a real timestamp to judge node health
        # against (see applyNodeHealthDot/refreshNodeHealth in
        # dashboard.html) instead of assuming "just arrived = healthy".
        self._push("pi_status", {"pi": pi_name, "data": data, "last_seen": last_seen})

    def _on_df_bearing(self, topic: str, data):
        with STATE_LOCK:
            STATE["df"]["bearing"]    = data.get("bearing") if isinstance(data, dict) else data
            STATE["df"]["confidence"] = data.get("confidence", 0) if isinstance(data, dict) else 0
            STATE["df"]["updated"]    = time.time()
        self._push("df_update", STATE["df"])

    def _on_radar_contact(self, topic: str, data):
        contact_id = topic.split("/")[-1]
        with STATE_LOCK:
            STATE["radar"]["contacts"][contact_id] = data
            STATE["radar"]["updated"] = time.time()
        self._push("radar_update", {"id": contact_id, "contact": data})

    def _on_rx_status(self, topic: str, data):
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["rx"].update({
                    "recording":     data.get("recording", False),
                    "last_file":     data.get("last_file", ""),
                    "last_trigger":  data.get("last_trigger", ""),
                    "capture_count": data.get("capture_count", 0),
                })
        self._push("rx_status", STATE["rx"])

    def _on_rx_event(self, topic: str, data):
        self._push("rx_event", data)

    def _on_rx_recording(self, topic: str, data):
        with STATE_LOCK:
            STATE["rx"]["recording"] = (str(data) == "1")
        self._push("rx_recording", {"active": STATE["rx"]["recording"]})

    def _on_tx_status(self, topic: str, data):
        with STATE_LOCK:
            STATE["tx"]["status"] = data
        self._push("tx_status", {"status": data})

    def _on_sdr_status(self, topic: str, data):
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["sdr"]["devices"] = data.get("devices", {})
        self._push("sdr_status", STATE["sdr"])

    def _on_thermal(self, topic: str, data):
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["thermal"].update(data)
        self._push("thermal", STATE["thermal"])

    def _on_alarm(self, topic: str, data):
        # entry carries a real timestamp (when this service received the
        # MQTT publish) alongside the alarm payload, and the same shape is
        # used for the live push and the state.alarms replay on reconnect -
        # the dashboard previously stamped alarms with whatever time the
        # browser happened to render them, and the live push dropped the
        # timestamp entirely (only `data` was sent).
        entry = {"time": time.time(), "data": data}
        with STATE_LOCK:
            STATE["alarms"].append(entry)
            STATE["alarms"] = STATE["alarms"][-20:]
        self._push("alarm", entry)
        log.warning("ALARM: %s", data)

    def _on_system_state(self, topic: str, data):
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["system"].update(data)

    def _on_ward_episode(self, topic: str, data):
        # WardService publishes the full open-episode summary retained on
        # shtf/ward/episode (specter_ward.py's WardService._publish_all) -
        # relay it straight through rather than reshaping it here, so the
        # UI's episode shape stays defined in exactly one place.
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["ward"]["episodes"] = data.get("episodes", [])
                STATE["ward"]["updated"] = time.time()
        self._push("ward_episode", STATE["ward"])

    def _on_ward_alert(self, topic: str, data):
        if isinstance(data, list):
            for alert in data:
                self._on_alarm(topic, alert)

    def _on_mesh_status(self, topic: str, data):
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["mesh"]["status"] = data.get("state", "unknown")
                STATE["mesh"]["updated"] = time.time()
        self._push("mesh_status", STATE["mesh"])

    def _on_mesh_inbound(self, topic: str, data):
        # specter_mesh_relay.py publishes one {"from","text","timestamp_utc"}
        # object per inbound mesh message (not retained) - keep a bounded
        # rolling log the same way STATE["alarms"] does, rather than growing
        # unbounded for a long-running dashboard process.
        if not isinstance(data, dict):
            return
        with STATE_LOCK:
            STATE["mesh"]["messages"].append(data)
            STATE["mesh"]["messages"] = STATE["mesh"]["messages"][-50:]
        self._push("mesh_message", data)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        data  = self._parse(msg.payload)

        for pattern, handler_name in self.TOPIC_MAP.items():
            if self._match(pattern, topic):
                getattr(self, handler_name)(topic, data)
                return

    @staticmethod
    def _match(pattern: str, topic: str) -> bool:
        p_parts = pattern.split("/")
        t_parts = topic.split("/")
        if len(p_parts) != len(t_parts):
            return False
        return all(p == "+" or p == t for p, t in zip(p_parts, t_parts))

    def _set_mqtt_connected(self, connected: bool) -> None:
        with STATE_LOCK:
            STATE["mqtt_connected"] = connected
        socketio.emit("mqtt_status", {"connected": connected})

    def _on_broker_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected")
            client.subscribe("shtf/#")
            self._set_mqtt_connected(True)
        else:
            log.warning("MQTT connect failed, rc=%s", rc)
            self._set_mqtt_connected(False)

    def _on_broker_disconnect(self, client, userdata, rc):
        log.warning("MQTT disconnected (rc=%s)", rc)
        self._set_mqtt_connected(False)

    # Commands the browser is allowed to trigger via ward_command - an
    # extra allowlist on top of the broker ACL (which already restricts
    # this credential's write access to exactly shtf/ward/command/#),
    # since a typo'd or attacker-supplied cmd string should be rejected
    # here rather than trusted through to a raw topic join.
    WARD_COMMANDS = {
        "open_episode", "close_episode", "intake", "output", "add_care_task",
        "complete_task", "skin_check", "nutrition", "mobility", "vitals",
    }

    def publish_ward_command(self, cmd: str, payload: dict) -> bool:
        if cmd not in self.WARD_COMMANDS:
            log.warning("Rejected unknown ward_command: %r", cmd)
            return False
        if not self._client:
            log.warning("Cannot publish ward command %s - MQTT not connected", cmd)
            return False
        try:
            self._client.publish(f"shtf/ward/command/{cmd}", json.dumps(payload), qos=1)
            return True
        except Exception as e:
            log.warning("Failed to publish ward command %s: %s", cmd, e)
            return False

    def start(self):
        import paho.mqtt.client as mqtt
        client = mqtt.Client(client_id="specter_dashboard")
        client.username_pw_set(self.username, self.password)
        # The footer's MQTT indicator was previously wired to nothing and
        # permanently read "MQTT: —" regardless of whether the broker
        # connection was actually up - on_connect/on_disconnect now push
        # real state instead of a static placeholder.
        client.on_connect = self._on_broker_connect
        client.on_disconnect = self._on_broker_disconnect
        client.on_message = self._on_message
        try:
            client.connect(self.broker, self.port, 60)
            client.loop_start()
            self._client = client
            log.info("Dashboard MQTT subscribed to shtf/#")
        except Exception as e:
            log.warning("MQTT unavailable: %s — dashboard running offline", e)
            self._set_mqtt_connected(False)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    dash_cfg    = cfg.get("dashboard", {})
    mqtt_cfg    = cfg.get("mqtt", {})
    service_cfg = mqtt_cfg.get("services", {}).get(MQTT_SERVICE_KEY)
    host = dash_cfg.get("host", "0.0.0.0")
    port = dash_cfg.get("port", 5000)

    if service_cfg:
        mqtt_username = service_cfg.get("username", MQTT_DEFAULT_USERNAME)
        mqtt_password = service_cfg.get("password", MQTT_DEFAULT_PASSWORD)
    else:
        # No dedicated services.<key> entry - do NOT fall back to the
        # broad "operator" credential (mqtt.username/password). The
        # dashboard's own ACL is already broad-READ by design, but
        # "operator" is readWRITE on shtf/# - falling back to it would
        # still hand this service write access (including trigger_rx-
        # adjacent topics) its own ACL never grants. Fall to this
        # service's own documented default instead - on a real broker its
        # password won't match the real (derived) one for this account,
        # so the connection is rejected rather than silently succeeding
        # with elevated access.
        log.error(
            "specter.json has no mqtt.services.%s entry - using this "
            "service's own default credential (which will fail to "
            "authenticate against a real broker) instead of the broad "
            "operator account. Re-run deploy/install_specter.py.",
            MQTT_SERVICE_KEY,
        )
        mqtt_username = MQTT_DEFAULT_USERNAME
        mqtt_password = MQTT_DEFAULT_PASSWORD

    mqtt = DashboardMQTT(
        broker   = mqtt_cfg.get("broker", "192.168.1.1"),
        port     = mqtt_cfg.get("port", 1883),
        username = mqtt_username,
        password = mqtt_password,
    )
    global _dashboard_mqtt
    _dashboard_mqtt = mqtt
    mqtt.start()

    def _stop(sig, frame):
        log.info("Dashboard server stopping")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("SPECTER Dashboard Server starting on http://%s:%d", host, port)
    socketio.run(app, host=host, port=port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
