#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║             SPECTER — MQTT COORDINATOR  (mqtt_coordinator.py)               ║
║                                                                              ║
║  Inter-Pi coordination hub. Runs on Pi 1 (Master).                         ║
║  • Subscribes to all shtf/* topics                                          ║
║  • Maintains live system state dict                                          ║
║  • Forwards alarms, DF bearings, radar contacts to dashboard                ║
║  • Publishes system heartbeat                                                ║
║  • Detects Pi dropouts and raises alarms                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

CONFIG_PATH = Path("/etc/specter/specter.json")
VERSION = "1.0.0"

# See docs/MANUAL.md Part 3.3 - the broker requires auth; these are the
# fallback credentials used only when specter.json has no mqtt.username/
# mqtt.password (e.g. running outside a real install).
MQTT_DEFAULT_USERNAME = "specter"
MQTT_DEFAULT_PASSWORD = "specter-change-me"

TOPIC_WILDCARD    = "shtf/#"
TOPIC_HEARTBEAT   = "shtf/system/heartbeat"
TOPIC_ALARM       = "shtf/system/alarm"
TOPIC_STATE       = "shtf/system/state"
PI_TIMEOUT_SEC    = 30   # declare Pi dead after this many seconds of silence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-coord] %(message)s",
)
log = logging.getLogger("specter.coordinator")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {"mqtt": {"broker": "192.168.1.1", "port": 1883}}


class MQTTCoordinator:
    def __init__(self):
        cfg = load_config()
        mqtt_cfg = cfg.get("mqtt", {})
        self.broker   = mqtt_cfg.get("broker", "192.168.1.1")
        self.port     = mqtt_cfg.get("port", 1883)
        self.username = mqtt_cfg.get("username", MQTT_DEFAULT_USERNAME)
        self.password = mqtt_cfg.get("password", MQTT_DEFAULT_PASSWORD)

        self.state: dict = {
            "pis": {},
            "df_bearing": None,
            "radar_contacts": {},
            "rx_recording": False,
            "tx_status": "idle",
            "alarms": [],
            "coordinator_uptime": 0,
        }
        self._start_time = time.time()
        self._stop_event = threading.Event()
        self._client     = None
        self._lock       = threading.Lock()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("Connected to MQTT broker %s:%d", self.broker, self.port)
            client.subscribe(TOPIC_WILDCARD)
            log.info("Subscribed to %s", TOPIC_WILDCARD)
        else:
            log.error("MQTT connect failed rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode(errors="replace").strip()

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = payload

        with self._lock:
            # Pi heartbeats  shtf/pi/<name>/status
            if topic.startswith("shtf/pi/") and topic.endswith("/status"):
                pi_name = topic.split("/")[2]
                self.state["pis"][pi_name] = {
                    "last_seen": time.time(),
                    "data": data,
                }

            # DF bearing
            elif topic == "shtf/df/bearing":
                self.state["df_bearing"] = data

            # Radar contacts
            elif topic.startswith("shtf/radar/contacts/"):
                contact_id = topic.split("/")[-1]
                self.state["radar_contacts"][contact_id] = data

            # RX recording status
            elif topic == "shtf/rx/recording":
                self.state["rx_recording"] = (str(data) == "1")

            # TX status
            elif topic == "shtf/tx/status":
                self.state["tx_status"] = data

            # Alarms — keep last 20
            elif topic == TOPIC_ALARM:
                self.state["alarms"].append({
                    "time": time.time(),
                    "payload": data,
                })
                self.state["alarms"] = self.state["alarms"][-20:]
                log.warning("ALARM: %s", data)

    def _watchdog_loop(self):
        """Detect Pi timeouts and raise alarms."""
        while not self._stop_event.is_set():
            now = time.time()
            with self._lock:
                for pi_name, info in self.state["pis"].items():
                    age = now - info.get("last_seen", 0)
                    if age > PI_TIMEOUT_SEC:
                        alarm = {
                            "source": "coordinator",
                            "level": "critical",
                            "msg": f"{pi_name} timeout — last seen {age:.0f}s ago",
                        }
                        if self._client:
                            self._client.publish(TOPIC_ALARM, json.dumps(alarm))
                        log.error("Pi timeout: %s (%.0fs)", pi_name, age)
            self._stop_event.wait(10)

    def _heartbeat_loop(self):
        """Publish coordinator heartbeat and full state snapshot."""
        while not self._stop_event.is_set():
            with self._lock:
                self.state["coordinator_uptime"] = int(time.time() - self._start_time)
                snapshot = dict(self.state)
            if self._client:
                self._client.publish(TOPIC_STATE, json.dumps(snapshot))
                self._client.publish(TOPIC_HEARTBEAT, json.dumps({
                    "service": "coordinator",
                    "version": VERSION,
                    "uptime": snapshot["coordinator_uptime"],
                    "pi_count": len(snapshot["pis"]),
                }))
            self._stop_event.wait(10)

    def run(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(client_id="specter_coordinator")
        client.username_pw_set(self.username, self.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client

        client.connect(self.broker, self.port, 60)
        client.loop_start()

        threads = [
            threading.Thread(target=self._watchdog_loop,   daemon=True, name="watchdog"),
            threading.Thread(target=self._heartbeat_loop,  daemon=True, name="heartbeat"),
        ]
        for t in threads:
            t.start()

        log.info("SPECTER MQTT Coordinator running. Broker: %s:%d", self.broker, self.port)

        def _stop(sig, frame):
            log.info("Signal received — stopping coordinator")
            self._stop_event.set()
            client.loop_stop()
            client.disconnect()
            sys.exit(0)

        signal.signal(signal.SIGINT,  _stop)
        signal.signal(signal.SIGTERM, _stop)

        while not self._stop_event.is_set():
            time.sleep(1)


def main() -> int:
    coord = MQTTCoordinator()
    coord.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
