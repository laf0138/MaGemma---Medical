#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           SPECTER — DASHBOARD SERVER  (dashboard_server.py)                 ║
║                                                                              ║
║  Flask + Socket.IO dashboard backend. Runs on Pi 1 (Master).               ║
║  • Subscribes to all MQTT topics                                             ║
║  • Pushes live updates to dashboard.html via WebSocket                      ║
║  • Serves dashboard.html to the local HTTPS reverse proxy                   ║
║  • Exposes /api/state and /api/status JSON endpoints                        ║
║  • <250ms DF bearing update latency                                         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import secrets
import signal
import sys
import threading
import time
from functools import wraps
from pathlib import Path

from flask import (
    Flask, jsonify, redirect, render_template_string, request, send_from_directory,
    session, url_for,
)
from flask_socketio import SocketIO, emit

CONFIG_PATH = Path("/etc/specter/specter.json")
DASHBOARD_DIR = Path(__file__).parent
VERSION = "1.2.0"
START_TIME = time.time()
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000

# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "dashboard"
# account: broad READ across shtf/# to drive the UI, zero WRITE - a
# leaked dashboard credential can only observe, never forge a command.
# These constants identify the required account and detect the installer's
# placeholder password. Runtime connections never fall back to it.
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
            "dashboard": {"host": "127.0.0.1", "port": 5000},
        }

cfg = load_config()


def hash_dashboard_password(password: str, *, salt: str | None = None) -> str:
    """Return the portable password-hash format written by the installer."""
    if not password:
        raise ValueError("dashboard password must not be empty")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PASSWORD_ITERATIONS
    ).hex()
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_dashboard_password(password: str, encoded: str) -> bool:
    """Verify an installer-generated hash without leaking timing information."""
    try:
        scheme, iterations_text, salt, expected = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        return False

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
    "medical": {"ecg_analysis": {}, "ecg_status": {}, "updated": 0},
    "alarms": [],
    "system": {"uptime": 0, "version": VERSION},
    "mqtt_connected": False,
}
STATE_LOCK = threading.Lock()
_dashboard_mqtt = None

# ─── Flask / SocketIO ─────────────────────────────────────────────────────────

# Do not let Flask's automatic /static route expose dashboard.html,
# resus.html, or this server's source without passing through authentication.
# Vendor assets have an explicit authenticated route below.
app = Flask(__name__, static_folder=None)
# A hardcoded secret here would be the same value in every SPECTER install
# (it's in the git repo) - functionally no secret at all. Prefer an
# operator-set value from specter.json (dashboard.secret_key, written once
# by the installer) so it's install-specific and stable across restarts;
# fall back to a random one generated at each startup, which is still a
# real per-process secret, but invalidates all operator sessions on restart.
app.config["SECRET_KEY"] = cfg.get("dashboard", {}).get("secret_key") or secrets.token_hex(32)
dashboard_auth_cfg = cfg.get("dashboard", {}).get("auth", {})
app.config.update(
    DASHBOARD_AUTH_USERNAME=dashboard_auth_cfg.get("username", ""),
    DASHBOARD_AUTH_PASSWORD_HASH=dashboard_auth_cfg.get("password_hash", ""),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=bool(cfg.get("dashboard", {}).get("cookie_secure", True)),
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)
# cors_allowed_origins="*" let ANY origin's page drive this dashboard's
# WebSocket (including trigger_rx) via a browser that merely has LAN
# access to it. Default to flask-socketio's same-origin-only behavior
# (cors_allowed_origins=None) unless the installer explicitly configures a
# list of trusted origins (e.g. a separate kiosk host) in specter.json.
socketio = SocketIO(
    app,
    cors_allowed_origins=cfg.get("dashboard", {}).get("cors_allowed_origins"),
    # Flask-SocketIO's maintained threading/simple-websocket backend avoids
    # Eventlet, which is deprecated and maintained in bug-fix mode only.
    async_mode="threading",
)


LOGIN_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SPECTER operator login</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#050a0f;color:#c8e0f0;font:16px monospace}
form{width:min(420px,calc(100vw - 40px));padding:28px;border:1px solid #00c8ff;background:#0a141e}
h1{color:#00c8ff;letter-spacing:.22em;font-size:22px}label{display:block;margin:18px 0 6px}
input,button{box-sizing:border-box;width:100%;min-height:48px;font:inherit;padding:10px;border:1px solid #4a6070;background:#050a0f;color:#fff}
button{margin-top:22px;border-color:#00c8ff;color:#00c8ff;cursor:pointer}input:focus,button:focus{outline:3px solid #ffaa00;outline-offset:2px}
.error{color:#ff6b73}.note{color:#8fa8b8;font-size:13px;line-height:1.5}</style></head>
<body><form method="post" action="{{ url_for('login', next=next_path) }}" autocomplete="on">
<h1>SPECTER</h1><p>Operator authentication required.</p>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<label for="username">Username</label><input id="username" name="username" required autofocus autocomplete="username">
<label for="password">Password</label><input id="password" name="password" type="password" required autocomplete="current-password">
<button type="submit">AUTHENTICATE</button>
<p class="note">Credentials remain local to this SPECTER node. Sessions expire after eight hours.</p>
</form></body></html>"""


def _auth_configured() -> bool:
    username = app.config.get("DASHBOARD_AUTH_USERNAME")
    password_hash = app.config.get("DASHBOARD_AUTH_PASSWORD_HASH")
    return bool(
        isinstance(username, str) and username
        and isinstance(password_hash, str) and password_hash
    )


def _is_authenticated() -> bool:
    return _auth_configured() and session.get("dashboard_authenticated") is True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_authenticated():
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path in {"/", "/resus", "/ward", "/login"} or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_configured():
        return (
            "<h1>SPECTER authentication is not configured</h1>"
            "<p>Run the installer with SPECTER_DASHBOARD_PASSWORD set.</p>",
            503,
        )

    csrf_token = session.get("login_csrf") or secrets.token_urlsafe(32)
    session["login_csrf"] = csrf_token
    error = None
    if request.method == "POST":
        submitted_csrf = request.form.get("csrf_token", "")
        username_ok = hmac.compare_digest(
            request.form.get("username", ""), app.config["DASHBOARD_AUTH_USERNAME"]
        )
        password_ok = verify_dashboard_password(
            request.form.get("password", ""), app.config["DASHBOARD_AUTH_PASSWORD_HASH"]
        )
        if (
            hmac.compare_digest(submitted_csrf, csrf_token)
            and username_ok
            and password_ok
        ):
            session.clear()
            session["dashboard_authenticated"] = True
            session.permanent = True
            destination = request.args.get("next", "/")
            if not destination.startswith("/") or destination.startswith("//"):
                destination = "/"
            return redirect(destination)
        error = "Invalid username, password, or form token."

    return render_template_string(
        LOGIN_TEMPLATE,
        csrf_token=csrf_token,
        error=error,
        next_path=request.args.get("next", "/"),
    )


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    html_path = DASHBOARD_DIR / "dashboard.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER Dashboard</h1><p>dashboard.html not found.</p>", 404


@app.route("/resus")
@login_required
def resus():
    html_path = DASHBOARD_DIR / "resus.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER RESUS</h1><p>resus.html not found.</p>", 404


@app.route("/ward")
@login_required
def ward():
    html_path = DASHBOARD_DIR / "ward.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER WARD</h1><p>ward.html not found.</p>", 404


@app.route("/dashboard/vendor/<path:filename>")
@login_required
def dashboard_vendor(filename: str):
    return send_from_directory(DASHBOARD_DIR / "vendor", filename)


@app.route("/api/state")
@login_required
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
def on_connect(auth=None):
    if not _is_authenticated():
        return False
    log.info("WebSocket client connected")
    with STATE_LOCK:
        emit("state", dict(STATE))


@socketio.on("trigger_rx")
def on_trigger_rx():
    """Operator presses RX capture button on dashboard."""
    if not _is_authenticated():
        log.warning("Rejected unauthenticated RX trigger")
        return False
    trigger = Path("/run/specter/sdr_trigger")
    try:
        trigger.touch()
        log.info("RX trigger sent via dashboard")
    except Exception as e:
        log.warning("Trigger file write failed: %s", e)


@socketio.on("ward_command")
def on_ward_command(data):
    """Relay authenticated, allowlisted WARD actions to the ward service."""
    if not _is_authenticated():
        log.warning("Rejected unauthenticated WARD command")
        return False
    if not isinstance(data, dict) or "cmd" not in data:
        log.warning("Malformed ward_command payload: %r", data)
        return False
    cmd = data["cmd"]
    payload = {key: value for key, value in data.items() if key != "cmd"}
    if _dashboard_mqtt is None:
        log.warning("ward_command %s dropped - MQTT not initialized", cmd)
        return False
    return _dashboard_mqtt.publish_ward_command(cmd, payload)


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
        "shtf/medical/ecg/status": "_on_ecg_status",
        "shtf/medical/ecg_analysis/+": "_on_ecg_analysis",
    }

    def __init__(self, broker: str, port: int, username: str, password: str):
        if not username or not password or password == MQTT_DEFAULT_PASSWORD:
            raise RuntimeError("dedicated MQTT credentials missing for dashboard")
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

    def _on_ecg_status(self, topic: str, data):
        if not isinstance(data, dict):
            return
        with STATE_LOCK:
            STATE["medical"]["ecg_status"] = data
            STATE["medical"]["updated"] = time.time()
        self._push("ecg_status", data)

    def _on_ecg_analysis(self, topic: str, data):
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or data.get("status") not in {"complete", "models_unavailable"}
        ):
            return
        patient_id = topic.split("/")[-1]
        if data.get("patient_id") != patient_id:
            log.warning("Rejected mismatched ECG analysis topic/payload patient")
            return
        # Retain the complete structured result for the authenticated UI;
        # original waveform arrays remain in the ECG archive, not browser RAM.
        with STATE_LOCK:
            STATE["medical"]["ecg_analysis"][patient_id] = data
            STATE["medical"]["updated"] = time.time()
        self._push("ecg_analysis", {"patient_id": patient_id, "analysis": data})

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

    def _on_broker_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("MQTT connected")
            client.subscribe("shtf/#")
            self._set_mqtt_connected(True)
        else:
            log.warning("MQTT connect failed: %s", reason_code)
            self._set_mqtt_connected(False)

    def _on_broker_disconnect(
        self, client, userdata, disconnect_flags_or_reason_code,
        reason_code=None, properties=None,
    ):
        reason_code = (
            disconnect_flags_or_reason_code if reason_code is None else reason_code
        )
        log.warning("MQTT disconnected: %s", reason_code)
        self._set_mqtt_connected(False)

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
            self._client.publish(
                f"shtf/ward/command/{cmd}", json.dumps(payload), qos=1
            )
            return True
        except Exception as exc:
            log.warning("Failed to publish ward command %s: %s", cmd, exc)
            return False

    def start(self):
        import paho.mqtt.client as mqtt
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id="specter_dashboard"
            )
        except (AttributeError, TypeError):
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
    service_cfg = mqtt_cfg.get("services", {}).get(MQTT_SERVICE_KEY, {})
    mqtt_username = service_cfg.get("username")
    mqtt_password = service_cfg.get("password")
    if not mqtt_username or not mqtt_password or mqtt_password == MQTT_DEFAULT_PASSWORD:
        raise RuntimeError("dedicated MQTT credentials missing for dashboard")
    host = dash_cfg.get("host", "127.0.0.1")
    port = dash_cfg.get("port", 5000)

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

    log.info("SPECTER Dashboard backend starting on http://%s:%d (HTTPS via Nginx)", host, port)
    socketio.run(app, host=host, port=port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
