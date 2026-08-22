#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║             SPECTER — THERMAL MONITOR  (thermal_monitor.py)                 ║
║                                                                              ║
║  Monitors CPU temperature on all Pis.                                       ║
║  • Publishes temps to MQTT every 10s                                        ║
║  • Raises alarms at threshold crossings                                      ║
║  • Issues emergency shutdown at 85°C (software) / warns at 75°C            ║
║                                                                              ║
║  Thresholds:                                                                 ║
║    Idle target:     < 45°C                                                   ║
║    Throttle warn:   ≥ 75°C                                                   ║
║    Emergency halt:  ≥ 85°C  (software) → hardware cut at ~95°C             ║
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
VERSION = "1.2.0"

# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "thermal" account:
# it can only write shtf/system/thermal and shtf/system/alarm - no read
# access at all. Runtime connections require the dedicated credential and
# reject the installer's placeholder password.
MQTT_SERVICE_KEY      = "thermal"
MQTT_DEFAULT_USERNAME = "specter-thermal"
MQTT_DEFAULT_PASSWORD = "specter-change-me"

TOPIC_THERMAL = "shtf/system/thermal"
TOPIC_ALARM   = "shtf/system/alarm"

TEMP_THROTTLE_C  = 75.0
TEMP_SHUTDOWN_C  = 85.0
POLL_INTERVAL    = 10  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-thermal] %(message)s",
)
log = logging.getLogger("specter.thermal")


def _mqtt_client(mqtt_module, client_id: str):
    try:
        return mqtt_module.Client(
            mqtt_module.CallbackAPIVersion.VERSION2, client_id=client_id
        )
    except (AttributeError, TypeError):
        return mqtt_module.Client(client_id=client_id)


def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg.get("thermal", {})
    except Exception:
        return {}


def read_cpu_temp() -> float:
    """Read CPU temperature in degrees C. Returns -1.0 on failure."""
    # Pi 5 / Pi 4 / Pi 3 standard path
    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        if thermal_path.exists():
            return int(thermal_path.read_text().strip()) / 1000.0
    except Exception:
        pass
    # Fallback: vcgencmd (Pi-specific)
    try:
        r = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=3)
        m = __import__("re").search(r"temp=([\d.]+)", r.stdout)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return -1.0


def read_gpu_temp() -> float:
    """Read GPU temperature if available (Pi-specific)."""
    try:
        r = subprocess.run(["vcgencmd", "measure_temp", "pmic"], capture_output=True, text=True, timeout=3)
        m = __import__("re").search(r"temp=([\d.]+)", r.stdout)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return -1.0


def read_throttle_flags() -> dict:
    """Read Pi throttle status via vcgencmd."""
    flags = {"throttled": False, "under_voltage": False, "freq_capped": False}
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        m = __import__("re").search(r"0x([0-9a-fA-F]+)", r.stdout)
        if m:
            val = int(m.group(1), 16)
            flags["under_voltage"] = bool(val & 0x1)
            flags["freq_capped"]   = bool(val & 0x2)
            flags["throttled"]     = bool(val & 0x4)
    except Exception:
        pass
    return flags


class ThermalMonitor:
    def __init__(self):
        cfg = load_config()
        self._throttle_c = cfg.get("throttle_c", TEMP_THROTTLE_C)
        self._shutdown_c = cfg.get("emergency_shutdown_c", TEMP_SHUTDOWN_C)

        self._stop_event  = threading.Event()
        self._client      = None
        self._start_time  = time.time()
        self._alarm_sent: dict[str, float] = {}

        try:
            raw_cfg = json.loads(CONFIG_PATH.read_text())
        except Exception as exc:
            raise RuntimeError("MQTT configuration is unreadable") from exc
        if not isinstance(raw_cfg, dict):
            raise RuntimeError("MQTT configuration must be a JSON object")
        mqtt = raw_cfg.get("mqtt", {})
        service_cfg = mqtt.get("services", {}).get(MQTT_SERVICE_KEY, {})
        username, password = service_cfg.get("username"), service_cfg.get("password")
        if not username or not password or password == MQTT_DEFAULT_PASSWORD:
            raise RuntimeError(f"dedicated MQTT credentials missing for {MQTT_SERVICE_KEY}")
        self.broker = mqtt.get("broker", "192.168.1.1")
        self.port = mqtt.get("port", 1883)
        self.username, self.password = username, password

    def _publish(self, topic: str, payload) -> None:
        if not self._client:
            return
        try:
            self._client.publish(topic,
                json.dumps(payload) if not isinstance(payload, str) else payload)
        except Exception:
            pass

    def _alarm(self, key: str, msg: str, level: str = "warning") -> None:
        now = time.time()
        last = self._alarm_sent.get(key, 0)
        if now - last < 60:   # suppress repeat alarms for 60s
            return
        self._alarm_sent[key] = now
        log.warning("ALARM [%s]: %s", level, msg)
        self._publish(TOPIC_ALARM, {
            "source": "thermal_monitor",
            "level":  level,
            "msg":    msg,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def _monitor_loop(self):
        log.info("Thermal monitor started (throttle=%.0f°C shutdown=%.0f°C)",
                 self._throttle_c, self._shutdown_c)
        while not self._stop_event.is_set():
            cpu_c     = read_cpu_temp()
            gpu_c     = read_gpu_temp()
            throttle  = read_throttle_flags()

            payload = {
                "cpu_temp_c":   cpu_c,
                "gpu_temp_c":   gpu_c,
                "throttle":     throttle,
                "uptime_sec":   int(time.time() - self._start_time),
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._publish(TOPIC_THERMAL, payload)

            if cpu_c >= self._shutdown_c:
                self._alarm("shutdown", f"CRITICAL: CPU {cpu_c:.1f}°C ≥ {self._shutdown_c}°C — EMERGENCY HALT", "critical")
                log.critical("CPU %.1f°C — issuing emergency halt!", cpu_c)
                os.system("wall 'SPECTER: CPU OVERTEMP — EMERGENCY SHUTDOWN'")
                time.sleep(3)
                os.system("systemctl poweroff")
            elif cpu_c >= self._throttle_c:
                self._alarm("throttle", f"WARNING: CPU {cpu_c:.1f}°C ≥ {self._throttle_c}°C — throttling", "warning")

            if throttle.get("under_voltage"):
                self._alarm("undervolt", "Under-voltage detected — check power supply", "warning")

            self._stop_event.wait(POLL_INTERVAL)

    def run(self):
        try:
            import paho.mqtt.client as mqtt
            client = _mqtt_client(mqtt, "specter_thermal")
            client.username_pw_set(self.username, self.password)
            client.connect(self.broker, self.port, 60)
            client.loop_start()
            self._client = client
        except Exception as e:
            log.warning("MQTT unavailable: %s — thermal monitor running standalone", e)

        def _stop(sig, frame):
            log.info("Stopping thermal monitor")
            self._stop_event.set()
            sys.exit(0)

        signal.signal(signal.SIGINT,  _stop)
        signal.signal(signal.SIGTERM, _stop)

        self._monitor_loop()


def main() -> int:
    mon = ThermalMonitor()
    mon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
