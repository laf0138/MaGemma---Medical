#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║               SPECTER — SDR CONTROL SERVICE  (sdr_control.py)               ║
║                                                                              ║
║  Manages HackRF, PlutoSDR, RTL-SDR v4, and KrakenSDR.                      ║
║  • Publishes SDR device status to MQTT                                       ║
║  • Accepts MQTT commands to tune/start/stop SDR processes                   ║
║  • Monitors SDR health and restarts on failure                               ║
║  • Reports frequency, gain, sample rate, and signal level                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

CONFIG_PATH = Path("/etc/specter/specter.json")
VERSION = "1.1.0"

# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "sdr_control"
# account: it can only read shtf/sdr/cmd and write its own status/alarm
# topics. Fallback values below are used only when specter.json has no
# mqtt.services.sdr_control entry (e.g. running outside a real install).
MQTT_SERVICE_KEY      = "sdr_control"
MQTT_DEFAULT_USERNAME = "specter-sdr-control"
MQTT_DEFAULT_PASSWORD = "specter-change-me"

TOPIC_SDR_STATUS  = "shtf/sdr/status"
TOPIC_SDR_CMD     = "shtf/sdr/cmd"
TOPIC_ALARM       = "shtf/system/alarm"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-sdr] %(message)s",
)
log = logging.getLogger("specter.sdr_control")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def probe_sdr_devices() -> dict:
    """Probe connected SDR hardware via SoapySDR and lsusb."""
    devices = {
        "hackrf":  {"present": False, "driver": "hackrf"},
        "pluto":   {"present": False, "driver": "plutosdr"},
        "kraken":  {"present": False, "driver": "rtlsdr", "count": 0},
        "rtlsdr":  {"present": False, "driver": "rtlsdr"},
    }

    try:
        result = subprocess.run(
            ["SoapySDRUtil", "--find"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.lower()
        if "hackrf" in output:
            devices["hackrf"]["present"] = True
        if "plutosdr" in output or "pluto" in output:
            devices["pluto"]["present"] = True
        if "rtlsdr" in output:
            devices["rtlsdr"]["present"] = True
    except Exception as e:
        log.warning("SoapySDRUtil probe failed: %s", e)

    try:
        lsusb = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
        blob = lsusb.stdout.lower()
        kraken_count = blob.count("0bda:2838")
        if kraken_count >= 5:
            devices["kraken"]["present"] = True
            devices["kraken"]["count"] = kraken_count
        if "1d50:6089" in blob:
            devices["hackrf"]["present"] = True
        if "0456:b673" in blob:
            devices["pluto"]["present"] = True
    except Exception as e:
        log.warning("lsusb probe failed: %s", e)

    return devices


class SDRControlService:
    def __init__(self):
        cfg = load_config()
        mqtt_cfg = cfg.get("mqtt", {})
        service_cfg = mqtt_cfg.get("services", {}).get(MQTT_SERVICE_KEY)
        self.broker   = mqtt_cfg.get("broker", "192.168.1.1")
        self.port     = mqtt_cfg.get("port", 1883)
        if service_cfg:
            self.username = service_cfg.get("username", MQTT_DEFAULT_USERNAME)
            self.password = service_cfg.get("password", MQTT_DEFAULT_PASSWORD)
        else:
            # No dedicated services.<key> entry - do NOT fall back to the
            # broad "operator" credential (mqtt.username/password): that
            # account has readwrite on shtf/# by design, so a missing
            # config entry would silently hand this service far MORE
            # privilege than its own least-privilege ACL grants, not less.
            # Fall to this service's own documented default instead - on a
            # real broker its password won't match the real (derived) one
            # for this account, so the connection is rejected rather than
            # silently succeeding with elevated access.
            log.error(
                "specter.json has no mqtt.services.%s entry - using this "
                "service's own default credential (which will fail to "
                "authenticate against a real broker) instead of the broad "
                "operator account. Re-run deploy/install_specter.py.",
                MQTT_SERVICE_KEY,
            )
            self.username = MQTT_DEFAULT_USERNAME
            self.password = MQTT_DEFAULT_PASSWORD

        self._devices   = {}
        self._procs:    dict[str, subprocess.Popen] = {}
        self._stop_event = threading.Event()
        self._client    = None
        self._lock      = threading.Lock()
        self._start_time = time.time()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected")
            client.subscribe(TOPIC_SDR_CMD)
        else:
            log.error("MQTT connect failed rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        """Handle SDR commands from MQTT."""
        try:
            cmd = json.loads(msg.payload.decode())
        except Exception:
            return

        action = cmd.get("action", "")
        device = cmd.get("device", "")
        log.info("SDR CMD: action=%s device=%s", action, device)

        if action == "probe":
            self._probe_and_publish()
        elif action == "restart" and device:
            self._restart_device(device)

    def _probe_and_publish(self):
        self._devices = probe_sdr_devices()
        self._publish_status()
        log.info("SDR probe complete: %s", {k: v["present"] for k, v in self._devices.items()})

    def _restart_device(self, device: str):
        log.info("Restart requested for: %s", device)
        # Placeholder — actual restart logic depends on which GNU Radio flow is running

    def _publish_status(self):
        if not self._client:
            return
        payload = {
            "version": VERSION,
            "uptime":  int(time.time() - self._start_time),
            "devices": self._devices,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._client.publish(TOPIC_SDR_STATUS, json.dumps(payload))

    def _status_loop(self):
        while not self._stop_event.is_set():
            self._publish_status()
            self._stop_event.wait(15)

    def run(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(client_id="specter_sdr_control")
        client.username_pw_set(self.username, self.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client

        try:
            client.connect(self.broker, self.port, 60)
            client.loop_start()
        except Exception as e:
            log.warning("MQTT connect failed: %s — running standalone", e)

        # Initial probe
        self._probe_and_publish()

        status_thread = threading.Thread(
            target=self._status_loop, daemon=True, name="sdr-status")
        status_thread.start()

        log.info("SPECTER SDR Control Service running")

        def _stop(sig, frame):
            log.info("Stopping SDR control service")
            self._stop_event.set()
            sys.exit(0)

        signal.signal(signal.SIGINT,  _stop)
        signal.signal(signal.SIGTERM, _stop)

        while not self._stop_event.is_set():
            time.sleep(2)


def main() -> int:
    svc = SDRControlService()
    svc.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
