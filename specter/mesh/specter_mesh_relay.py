#!/usr/bin/env python3
"""
SPECTER Mesh Relay — internal comms + alarm notification over LoRa
Runs on: Node 1 (Pi 5, 192.168.1.1), attached to a Meshtastic-firmware
LoRa radio over USB serial (915MHz ISM band in the US region config).

Responsibilities:
  1. Watch trauma/ward/system alert topics and relay new or changed alarms
     onto the LoRa mesh, so a critical event reaches operators who are not
     looking at the dashboard (or who are off-grid from the LAN entirely).
  2. Relay inbound mesh text back onto MQTT (shtf/mesh/inbound) so it's
     visible wherever SPECTER's other state is - the dashboard, logs, etc.
  3. Accept operator-composed outbound messages via shtf/mesh/command/send,
     the same authenticated-command pattern trauma/ward already use.

Publishes to:  shtf/mesh/status   (retained - connected/disconnected/etc.)
               shtf/mesh/sent     (log of what was actually transmitted)
               shtf/mesh/inbound  (mesh text received from other nodes)
Subscribes to: shtf/trauma/alert, shtf/ward/alert, shtf/system/alarm,
               shtf/mesh/command/#

WHAT "ENCRYPTED" MEANS HERE
----------------------------
Encryption is Meshtastic's own channel model (AES-256-CTR with a
per-channel pre-shared key), configured on the physical radio itself - not
something this script manages. Provision the channel PSK once during
hardware setup with Meshtastic's own CLI/app (see docs/MANUAL.md); this
service just sends/receives whatever channel the radio is already
configured for. Re-implementing channel/key management here would
duplicate well-tested upstream tooling for no benefit and a real risk of
getting the crypto wrong - the same reasoning that keeps this module from
reinventing NEWS2 scoring or MQTT auth from scratch.

HARDWARE STATUS
----------------
Built against the real `meshtastic` PyPI package (v2.7.11 at the time of
writing) and its documented API (SerialInterface, sendAlert/sendText,
pypubsub receive topics) - not guessed. It has NOT been run against a real
Meshtastic radio; treat this the same as the medical hub's BLE devices
(docs/MANUAL.md Part 7.2/7.4): logic is real and reviewed against the
library's actual source, but unverified against real hardware. If nothing
is plugged in, this service idles cleanly (see MeshtasticHardware) rather
than crashing or blocking other SPECTER services - the mesh relay is a
notification channel, not a dependency anything else here relies on.

TRAINING GATE
-------------
This is a best-effort relay, not a guaranteed-delivery channel. Meshtastic
text sends here use wantAck=False (no delivery confirmation tracked) and
LoRa airtime is scarce and shared - see REALERT_INTERVAL_SECONDS below for
why a persistent alarm is only re-announced periodically, not every time
its source republishes. Do not treat "no reply on the mesh" as "the alert
didn't fire" or "everyone is fine" - it means exactly what it says: no
confirmation was received.

Author: SPECTER Build Team
Date: August 2026
Version: 1.2.0
"""

import json
import logging
import os
import argparse
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

# --- paho-mqtt 1.x / 2.x compatibility -------------------------------------
def _mqtt_client(client_id: str = ""):
    """Construct an MQTT client that works on paho-mqtt 1.x and 2.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x has no CallbackAPIVersion - fall back to the
        # old-style constructor (deprecated but functional on 2.x too),
        # NOT a recursive call to this same function.
        return mqtt.Client(client_id=client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "mesh" account: it
# can only read the alert/command topics and write its own status/sent/
# inbound topics, so a leaked credential from any other service can't
# forge mesh traffic, and a leaked mesh credential can't touch trauma/ward
# state. Runtime connections require the dedicated credential and reject
# the installer's placeholder password.
MQTT_SERVICE_KEY      = "mesh"
MQTT_DEFAULT_USERNAME = "specter-mesh"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple[str, str]:
    """Return only this service's dedicated credential, or fail closed."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
    except Exception as exc:
        raise RuntimeError("MQTT configuration is unreadable") from exc
    if not isinstance(cfg, dict):
        raise RuntimeError("MQTT configuration must be a JSON object")
    service_cfg = cfg.get("mqtt", {}).get("services", {}).get(MQTT_SERVICE_KEY, {})
    username, password = service_cfg.get("username"), service_cfg.get("password")
    if not username or not password or password == MQTT_DEFAULT_PASSWORD:
        raise RuntimeError(f"dedicated MQTT credentials missing for {MQTT_SERVICE_KEY}")
    return username, password
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get("SPECTER_LOG", "/var/log/specter/mesh.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("specter.mesh")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===========================================================================
# Hardware layer - isolated behind a thin wrapper so the relay/dedup/
# formatting logic below is unit-testable without a real LoRa radio.
# ===========================================================================

class MeshtasticHardware:
    """
    Thin wrapper around the `meshtastic` package's SerialInterface.

    IMPORTANT hardware-detection quirk, verified against meshtastic==2.7.11
    source rather than assumed: SerialInterface(devPath=None) does its own
    port-probing internally, and when it finds MORE than one candidate
    port it calls meshtastic.util.our_exit(...), which is a bare
    sys.exit(1) - not a catchable exception. Constructing SerialInterface
    that way on a Pi with more than one USB-serial device attached (easy
    to hit in practice - a GPS puck, another radio's CAT interface, etc.)
    would kill this entire systemd service with no chance to log or
    retry. This wrapper therefore always resolves devPath itself first
    and only ever constructs SerialInterface with an explicit,
    unambiguous path, so that code path is never reached.
    """

    def __init__(self, dev_path: Optional[str] = None):
        self.configured_dev_path = dev_path
        self.iface = None

    def resolve_port(self) -> Tuple[Optional[str], str]:
        """Resolve a serial device path without touching the radio.
        Returns (dev_path_or_None, status), status one of "ok",
        "not_found", "ambiguous"."""
        if self.configured_dev_path:
            return self.configured_dev_path, "ok"
        import meshtastic.util as mutil
        ports = mutil.findPorts(eliminate_duplicates=True)
        if len(ports) == 0:
            return None, "not_found"
        if len(ports) > 1:
            return None, "ambiguous"
        return ports[0], "ok"

    def connect(self):
        """Raises on failure - caller handles retry/backoff. Never lets an
        ambiguous/absent port reach meshtastic's own port-probing path
        (see class docstring)."""
        import meshtastic.serial_interface as si
        dev_path, status = self.resolve_port()
        if status != "ok":
            raise RuntimeError(f"mesh hardware {status}")
        self.iface = si.SerialInterface(devPath=dev_path)
        return self.iface

    def close(self) -> None:
        if self.iface is not None:
            try:
                self.iface.close()
            except Exception:
                pass
            self.iface = None


# ===========================================================================
# Alert normalization
# ===========================================================================
#
# shtf/trauma/alert and shtf/ward/alert each publish a JSON array of
# {"level", "text", ...} objects. shtf/system/alarm publishes a single
# JSON object per alarm, and its shape is NOT consistent across today's
# publishers - thermal_monitor.py uses "level" + "msg",
# specter_rx_ring_buffer.py omits "level" entirely and uses "msg". Rather
# than assume one shape (and silently drop the others), this accepts
# either a list or a dict and reads whichever of "text"/"msg" is present,
# defaulting a missing level to "caution" - never dropping a real alarm
# because of a field-naming mismatch this relay doesn't control.

def normalize_alerts(topic: str, payload: Any) -> List[Dict[str, str]]:
    source = topic.split("/")[1] if "/" in topic else "system"  # "trauma"/"ward"/"system"
    items = payload if isinstance(payload, list) else [payload]
    out: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("msg")
        if not text:
            continue
        out.append({
            "source": str(item.get("source", source)),
            "level": str(item.get("level") or "caution"),
            "text": str(text),
        })
    return out


# ===========================================================================
# Relay service
# ===========================================================================

class MeshRelayService:
    TOPIC_TRAUMA_ALERT = "shtf/trauma/alert"
    TOPIC_WARD_ALERT = "shtf/ward/alert"
    TOPIC_SYSTEM_ALARM = "shtf/system/alarm"
    TOPIC_COMMAND = "shtf/mesh/command/#"
    TOPIC_STATUS = "shtf/mesh/status"
    TOPIC_SENT = "shtf/mesh/sent"
    TOPIC_INBOUND = "shtf/mesh/inbound"

    # LoRa airtime is scarce and shared across the whole mesh, and at
    # Meshtastic's default LongFast preset a single message can take
    # seconds to transmit at long range. The ward module alone
    # republishes its full alert list every 30s regardless of whether
    # anything changed (specter_ward.py's _clock_loop) - relaying every
    # one of those unfiltered would flood the link. An UNCHANGED alert
    # set is therefore only re-announced this often, as a "still active"
    # reaffirmation. Any CHANGE (new alert, cleared alert, different
    # alert set) always sends immediately regardless of this interval.
    REALERT_INTERVAL_SECONDS = 900

    # Conservative vs. Meshtastic's practical text-payload ceiling (varies
    # by firmware/region config, commonly ~200-237 bytes) - not verified
    # against real hardware, so this stays deliberately conservative.
    MAX_MESSAGE_CHARS = 200

    HW_RETRY_SECONDS = 30

    def __init__(self, mqtt_host: str, mqtt_port: int, serial_port: Optional[str] = None):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.hw = MeshtasticHardware(dev_path=serial_port)
        self._ready = threading.Event()
        self._stop = threading.Event()
        # source ("trauma"/"ward"/"system") -> (signature, last_sent_time)
        self._last_sent: Dict[str, Tuple[tuple, float]] = {}

        self.mqtt = _mqtt_client("specter-mesh")
        self.mqtt.username_pw_set(*_mqtt_credentials())
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    # -- MQTT lifecycle ------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe(self.TOPIC_TRAUMA_ALERT, qos=1)
            client.subscribe(self.TOPIC_WARD_ALERT, qos=1)
            client.subscribe(self.TOPIC_SYSTEM_ALARM, qos=1)
            client.subscribe(self.TOPIC_COMMAND, qos=1)
            logger.info("MQTT connected")
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode()) if msg.payload else {}
        except Exception:
            logger.warning("Non-JSON payload on %s, ignoring", msg.topic)
            return

        if msg.topic in (self.TOPIC_TRAUMA_ALERT, self.TOPIC_WARD_ALERT, self.TOPIC_SYSTEM_ALARM):
            self._handle_alert(msg.topic, payload)
        elif msg.topic.startswith("shtf/mesh/command/"):
            self._handle_command(msg.topic, payload)

    # -- Alert intake, dedup, formatting ---------------------------------

    def _handle_alert(self, topic: str, payload: Any) -> None:
        source = topic.split("/")[1]
        alerts = normalize_alerts(topic, payload)
        signature = tuple(sorted((a["level"], a["text"]) for a in alerts))
        now = time.time()
        prev_sig, prev_ts = self._last_sent.get(source, ((), 0.0))

        if signature == prev_sig:
            if signature and (now - prev_ts) >= self.REALERT_INTERVAL_SECONDS:
                self._send_alerts(source, alerts, reaffirm=True)
                self._last_sent[source] = (signature, now)
            return  # unchanged and within cooldown, or unchanged-and-empty

        if not signature:
            self._send_mesh_message(source, "text", f"SPECTER {source.upper()}: all clear, alerts resolved")
        else:
            self._send_alerts(source, alerts, reaffirm=False)
        self._last_sent[source] = (signature, now)

    def _send_alerts(self, source: str, alerts: List[Dict[str, str]], reaffirm: bool) -> None:
        critical = any(a["level"] == "critical" for a in alerts)
        text = self._format_alerts(source, alerts, reaffirm)
        self._send_mesh_message(source, "alert" if critical else "text", text)

    def _format_alerts(self, source: str, alerts: List[Dict[str, str]], reaffirm: bool) -> str:
        prefix = f"SPECTER {source.upper()}"
        if reaffirm:
            prefix += " (still active)"
        body = "; ".join(f"{a['level'].upper()}: {a['text']}" for a in alerts)
        msg = f"{prefix} - {body}"
        if len(msg) > self.MAX_MESSAGE_CHARS:
            msg = msg[: self.MAX_MESSAGE_CHARS - 1].rstrip() + "…"
        return msg

    # -- Operator-composed outbound commands -----------------------------

    def _handle_command(self, topic: str, payload: Dict[str, Any]) -> None:
        cmd = topic.split("/")[-1]
        if cmd != "send":
            logger.warning("Unknown mesh command: %s", cmd)
            return
        text = str(payload.get("text", "")).strip()
        if not text:
            return
        if len(text) > self.MAX_MESSAGE_CHARS:
            text = text[: self.MAX_MESSAGE_CHARS - 1].rstrip() + "…"
        kind = "alert" if payload.get("priority") == "alert" else "text"
        self._send_mesh_message("operator", kind, text)

    # -- Outbound send -----------------------------------------------------

    def _send_mesh_message(self, source: str, kind: str, text: str) -> None:
        if not self._ready.is_set() or self.hw.iface is None:
            logger.warning("Mesh not connected, dropping outbound message: %s", text)
            return
        try:
            if kind == "alert":
                self.hw.iface.sendAlert(text)
            else:
                self.hw.iface.sendText(text)
            logger.info("Mesh TX (%s): %s", kind, text)
            self.mqtt.publish(self.TOPIC_SENT, json.dumps({
                "source": source, "kind": kind, "text": text, "timestamp_utc": utcnow(),
            }), qos=1)
        except Exception:
            logger.exception("Mesh send failed")

    # -- Hardware lifecycle -----------------------------------------------

    def _hw_connect_loop(self) -> None:
        from pubsub import pub
        while not self._stop.is_set():
            dev_path, status = self.hw.resolve_port()
            if status != "ok":
                self._publish_status(status)
                if status == "not_found":
                    logger.warning(
                        "No Meshtastic LoRa hardware detected - retrying in %ss",
                        self.HW_RETRY_SECONDS,
                    )
                else:
                    logger.error(
                        "Multiple candidate serial ports for mesh hardware - "
                        "set an explicit --serial-port to disambiguate; "
                        "retrying in %ss",
                        self.HW_RETRY_SECONDS,
                    )
                self._stop.wait(self.HW_RETRY_SECONDS)
                continue
            try:
                self.hw.connect()
                pub.subscribe(self._on_mesh_text, "meshtastic.receive.text")
                pub.subscribe(self._on_mesh_connection_lost, "meshtastic.connection.lost")
                self._ready.set()
                self._publish_status("connected")
                logger.info("Mesh hardware connected on %s", dev_path)
                while not self._stop.is_set() and self._ready.is_set():
                    self._stop.wait(5)
            except Exception:
                logger.exception("Mesh hardware connect failed")
                self._publish_status("connect_failed")
                self._ready.clear()
                self.hw.close()
                self._stop.wait(self.HW_RETRY_SECONDS)

    def _on_mesh_connection_lost(self, interface=None) -> None:
        logger.warning("Mesh hardware connection lost")
        self._ready.clear()
        self._publish_status("disconnected")

    def _on_mesh_text(self, packet: Optional[dict] = None, interface=None) -> None:
        try:
            decoded = (packet or {}).get("decoded", {}) or {}
            text = decoded.get("text")
            if not text:
                return
            entry = {
                "from": (packet or {}).get("fromId", "unknown"),
                "text": text,
                "timestamp_utc": utcnow(),
            }
            logger.info("Mesh RX: %s: %s", entry["from"], text)
            self.mqtt.publish(self.TOPIC_INBOUND, json.dumps(entry), qos=1)
        except Exception:
            logger.exception("Failed to relay inbound mesh message")

    def _publish_status(self, state: str) -> None:
        self.mqtt.publish(self.TOPIC_STATUS, json.dumps({
            "state": state, "timestamp_utc": utcnow(),
        }), qos=1, retain=True)

    # -- Run -----------------------------------------------------------

    def start(self) -> None:
        self.mqtt.will_set(self.TOPIC_STATUS, json.dumps({"state": "offline"}), qos=1, retain=True)
        self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        threading.Thread(target=self._hw_connect_loop, daemon=True).start()
        self.mqtt.loop_forever()

    def stop(self) -> None:
        self._stop.set()
        self.hw.close()
        self._publish_status("offline")
        self.mqtt.disconnect()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="SPECTER Mesh Relay (Node 1)")
    ap.add_argument("--mqtt-host", default="192.168.1.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument(
        "--serial-port",
        default=None,
        help="Explicit serial device path for the LoRa radio (e.g. /dev/ttyUSB0). "
             "Omit to auto-detect - required only when more than one USB-serial "
             "device is attached and auto-detection is ambiguous.",
    )
    args = ap.parse_args()

    service = MeshRelayService(
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        serial_port=args.serial_port,
    )
    try:
        service.start()
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
