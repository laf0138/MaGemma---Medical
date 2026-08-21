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

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, send_from_directory
from flask_socketio import SocketIO, emit

CONFIG_PATH = Path("/etc/specter/specter.json")
DASHBOARD_DIR = Path(__file__).parent
VERSION = "1.0.0"

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

# ─── Shared state ─────────────────────────────────────────────────────────────

STATE: dict = {
    "pis": {},
    "df": {"bearing": None, "confidence": 0, "updated": 0},
    "radar": {"contacts": {}, "updated": 0},
    "rx": {"recording": False, "last_file": "", "last_trigger": "", "capture_count": 0},
    "tx": {"status": "idle", "frequency": None},
    "sdr": {"devices": {}},
    "thermal": {"cpu_temp_c": 0, "throttle": {}},
    "alarms": [],
    "system": {"uptime": 0, "version": VERSION},
}
STATE_LOCK = threading.Lock()

# ─── Flask / SocketIO ─────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=str(DASHBOARD_DIR))
app.config["SECRET_KEY"] = "specter-dashboard-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


@app.route("/")
def index():
    html_path = DASHBOARD_DIR / "dashboard.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>SPECTER Dashboard</h1><p>dashboard.html not found.</p>", 404


@app.route("/api/state")
def api_state():
    with STATE_LOCK:
        return jsonify(dict(STATE))


@app.route("/api/status")
def api_status():
    return jsonify({
        "service": "specter-dashboard",
        "version": VERSION,
        "uptime":  int(time.time()),
        "ok":      True,
    })


@socketio.on("connect")
def on_connect():
    log.info("WebSocket client connected")
    with STATE_LOCK:
        emit("state", dict(STATE))


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
    }

    def __init__(self, broker: str, port: int):
        self.broker = broker
        self.port   = port
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
        with STATE_LOCK:
            STATE["pis"][pi_name] = {"last_seen": time.time(), "data": data}
        self._push("pi_status", {"pi": pi_name, "data": data})

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
        with STATE_LOCK:
            STATE["alarms"].append({"time": time.time(), "data": data})
            STATE["alarms"] = STATE["alarms"][-20:]
        self._push("alarm", data)
        log.warning("ALARM: %s", data)

    def _on_system_state(self, topic: str, data):
        with STATE_LOCK:
            if isinstance(data, dict):
                STATE["system"].update(data)

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

    def start(self):
        import paho.mqtt.client as mqtt
        client = mqtt.Client(client_id="specter_dashboard")
        client.on_connect = lambda c, u, f, rc: (
            log.info("MQTT connected") or c.subscribe("shtf/#"))
        client.on_message = self._on_message
        try:
            client.connect(self.broker, self.port, 60)
            client.loop_start()
            self._client = client
            log.info("Dashboard MQTT subscribed to shtf/#")
        except Exception as e:
            log.warning("MQTT unavailable: %s — dashboard running offline", e)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    dash_cfg  = cfg.get("dashboard", {})
    mqtt_cfg  = cfg.get("mqtt", {})
    host = dash_cfg.get("host", "0.0.0.0")
    port = dash_cfg.get("port", 5000)

    mqtt = DashboardMQTT(
        broker = mqtt_cfg.get("broker", "192.168.1.1"),
        port   = mqtt_cfg.get("port", 1883),
    )
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
