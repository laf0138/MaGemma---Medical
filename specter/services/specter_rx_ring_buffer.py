#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          SPECTER RX ROLLING CAPTURE SERVICE  —  specter_rx_ring_buffer.py  ║
║                                                                              ║
║  Always-on demodulated-audio event recorder with pre-trigger memory,        ║
║  post-trigger capture, VOX/manual/MQTT/file-trigger activation,             ║
║  sidecar JSON metadata, and MQTT dashboard reporting.                        ║
║                                                                              ║
║  Part of the SPECTER Emergency Communications Command Center                ║
║  Target host: Raspberry Pi 5 (8GB) — Pi 1 (Master) or Pi 2 (TX/RX)        ║
║                                                                              ║
║  Paths:                                                                      ║
║    App:        /opt/specter/                                                 ║
║    Config:     /etc/specter/                                                 ║
║    Logs:       /var/log/specter/                                             ║
║    Recordings: /mnt/specter/live/recordings/                                ║
║    Trigger:    /run/specter/sdr_trigger   (RuntimeDirectory=specter)         ║
║    Venv:       /opt/specter/venv/                                            ║
║                                                                              ║
║  MQTT Topics (SPECTER namespace):                                            ║
║    shtf/rx/status       — heartbeat + service health                        ║
║    shtf/rx/event        — new capture event (filename, reason, timestamp)   ║
║    shtf/rx/recording    — recording active flag (0 / 1)                     ║
║    shtf/rx/trigger      — external trigger input (subscribe)                ║
║    shtf/system/alarm    — system-level alarm publish                        ║
║                                                                              ║
║  Systemd service:   specter_rx_ring_buffer.service                          ║
║  Install mode:      python3 specter_rx_ring_buffer.py install               ║
║  Run mode:          python3 specter_rx_ring_buffer.py run [options]         ║
║  Trigger mode:      python3 specter_rx_ring_buffer.py trigger               ║
║  List devices:      python3 specter_rx_ring_buffer.py list-devices          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

# ─── Optional deps — fail loudly at runtime if missing, not at import ────────
def _require(module_name: str, pip_name: str | None = None):
    """Import a module, emitting a clear error if missing."""
    import importlib
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pkg = pip_name or module_name
        print(f"[SPECTER] Missing dependency: {module_name}  →  pip install {pkg}", file=sys.stderr)
        sys.exit(1)


# ─── Constants & Defaults ─────────────────────────────────────────────────────

VERSION = "1.0.0"

APP_DIR          = Path("/opt/specter")
CONFIG_DIR       = Path("/etc/specter")
LOG_DIR          = Path("/var/log/specter")
RECORD_DIR       = Path("/mnt/specter/live/recordings")
VENV_DIR         = APP_DIR / "venv"
TRIGGER_FILE     = Path("/run/specter/sdr_trigger")   # systemd RuntimeDirectory=specter
SERVICE_NAME     = "specter_rx_ring_buffer.service"
DEFAULT_USER     = "specter"

DEFAULT_SAMPLE_RATE     = 48_000
DEFAULT_CHANNELS        = 1
DEFAULT_BUFFER_SECONDS  = 5        # pre-trigger rolling window
DEFAULT_POST_SECONDS    = 15       # post-trigger capture duration
DEFAULT_MAX_RECORD_SEC  = 300      # hard cap — prevents infinite VOX files
DEFAULT_CHUNK_SIZE      = 4_096
DEFAULT_QUEUE_DEPTH     = 512      # ~5.5 s @ 48kHz/4096; exposed as CLI arg

MQTT_BROKER_DEFAULT     = "192.168.1.1"
MQTT_PORT_DEFAULT       = 1883
MQTT_KEEPALIVE          = 60
MQTT_STATUS_INTERVAL    = 10       # seconds between heartbeats

TOPIC_STATUS     = "shtf/rx/status"
TOPIC_EVENT      = "shtf/rx/event"
TOPIC_RECORDING  = "shtf/rx/recording"
TOPIC_TRIGGER    = "shtf/rx/trigger"   # subscribed — external systems can fire this
TOPIC_ALARM      = "shtf/system/alarm"

SYSTEMD_SERVICE_TEMPLATE = """[Unit]
Description=SPECTER RX Rolling Capture Service
After=network.target sound.target

[Service]
Type=simple
User={user}
Group=audio
WorkingDirectory={app_dir}
ExecStart={venv_python} {script_path} run \\
    --output-dir {record_dir} \\
    --trigger-file {trigger_file} \\
    --mqtt-broker {mqtt_broker} \\
    --log-level INFO
Restart=always
RestartSec=5
RuntimeDirectory=specter
RuntimeDirectoryMode=0755
StandardOutput=journal
StandardError=journal
SyslogIdentifier=specter-rx

[Install]
WantedBy=multi-user.target
"""

# ─── Ring Buffer ──────────────────────────────────────────────────────────────

class RingBuffer:
    """
    Thread-safe rolling audio ring buffer (int16, flat array).

    Fixed-point: the 'filled' flag is set as soon as a write causes the
    write-position to wrap past the end of the buffer — not only when pos==0.
    This was the bug in the original rx_ring_buffer.py.
    """

    def __init__(self, sample_rate: int, seconds: int, channels: int):
        self.maxlen = sample_rate * seconds * channels
        self.buffer  = np.zeros(self.maxlen, dtype=np.int16)
        self.pos     = 0
        self.filled  = False
        self.lock    = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        with self.lock:
            n = len(data)
            if n == 0:
                return
            if n >= self.maxlen:
                self.buffer[:] = data[-self.maxlen:]
                self.pos    = 0
                self.filled = True
                return
            end     = self.pos + n
            wrapped = end >= self.maxlen          # ← FIX: detect any wrap, not just pos==0
            if not wrapped:
                self.buffer[self.pos:end] = data
            else:
                first = self.maxlen - self.pos
                self.buffer[self.pos:] = data[:first]
                self.buffer[:n - first] = data[first:]
            self.pos = end % self.maxlen
            if wrapped:                           # ← FIX: set filled on any wrap
                self.filled = True

    def read_all(self) -> np.ndarray:
        with self.lock:
            if not self.filled:
                return self.buffer[:self.pos].copy()
            return np.concatenate([self.buffer[self.pos:], self.buffer[:self.pos]])


# ─── MQTT Helper ──────────────────────────────────────────────────────────────

class MQTTClient:
    """
    Thin wrapper around paho-mqtt with auto-reconnect and a trigger callback.
    Gracefully degrades if paho-mqtt is not installed (MQTT simply disabled).
    """

    def __init__(self, broker: str, port: int, trigger_cb, log: logging.Logger):
        self.broker     = broker
        self.port       = port
        self.trigger_cb = trigger_cb
        self.log        = log
        self._client    = None
        self._connected = False
        self._lock      = threading.Lock()
        self._try_connect()

    def _try_connect(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.log.warning("paho-mqtt not installed — MQTT disabled. "
                             "Install with: pip install paho-mqtt")
            return

        client = mqtt.Client(client_id="specter_rx_ring_buffer")
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message
        try:
            client.connect(self.broker, self.port, MQTT_KEEPALIVE)
            client.loop_start()
            self._client = client
        except Exception as exc:
            self.log.warning("MQTT connect failed (%s:%d): %s — running without MQTT",
                             self.broker, self.port, exc)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected = True
            self.log.info("MQTT connected to %s:%d", self.broker, self.port)
            client.subscribe(TOPIC_TRIGGER)
        else:
            self.log.warning("MQTT connect refused (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        self.log.warning("MQTT disconnected (rc=%d) — will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic == TOPIC_TRIGGER:
            payload = msg.payload.decode(errors="replace").strip()
            self.log.info("MQTT trigger received: %s", payload)
            self.trigger_cb(reason=f"mqtt:{payload or 'remote'}")

    def publish(self, topic: str, payload, retain: bool = False) -> None:
        if self._client is None or not self._connected:
            return
        try:
            if not isinstance(payload, (str, bytes, bytearray)):
                payload = json.dumps(payload)
            self._client.publish(topic, payload, retain=retain)
        except Exception as exc:
            self.log.debug("MQTT publish error on %s: %s", topic, exc)

    def stop(self) -> None:
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._connected


# ─── RX Buffer Service ────────────────────────────────────────────────────────

class RXBufferService:
    """
    Core SPECTER RX Rolling Capture Service.

    Continuously reads audio from a PyAudio input device, maintains a
    pre-trigger ring buffer, and writes event WAV files (+ sidecar JSON)
    on trigger (file-touch, MQTT, VOX, or manual API call).
    """

    def __init__(
        self,
        device_index:    int   | None = None,
        sample_rate:     int          = DEFAULT_SAMPLE_RATE,
        channels:        int          = DEFAULT_CHANNELS,
        buffer_seconds:  int          = DEFAULT_BUFFER_SECONDS,
        post_seconds:    int          = DEFAULT_POST_SECONDS,
        max_record_sec:  int          = DEFAULT_MAX_RECORD_SEC,
        chunk_size:      int          = DEFAULT_CHUNK_SIZE,
        queue_depth:     int          = DEFAULT_QUEUE_DEPTH,
        output_dir:      str          = str(RECORD_DIR),
        trigger_file:    str          = str(TRIGGER_FILE),
        vox_threshold:   float | None = None,
        mqtt_broker:     str          = MQTT_BROKER_DEFAULT,
        mqtt_port:       int          = MQTT_PORT_DEFAULT,
        mqtt_enabled:    bool         = True,
        sdr_source:      str          = "audio-input",
        frequency_hz:    int   | None = None,
        log_level:       str          = "INFO",
    ):
        self.sample_rate    = sample_rate
        self.channels       = channels
        self.buffer_seconds = buffer_seconds
        self.post_seconds   = post_seconds
        self.max_record_sec = max_record_sec
        self.chunk_size     = chunk_size
        self.queue_depth    = queue_depth
        self.output_dir     = Path(output_dir)
        self.trigger_file   = Path(trigger_file)
        self.vox_threshold  = vox_threshold
        self.device_index   = device_index
        self.sdr_source     = sdr_source
        self.frequency_hz   = frequency_hz
        self.mqtt_enabled   = mqtt_enabled

        # Stats counters (published to MQTT)
        self._queue_overflow_count = 0
        self._capture_count        = 0
        self._vox_level            = 0.0
        self._last_capture_file    = ""
        self._last_trigger_reason  = ""
        self._service_start_time   = time.time()

        # Recording state
        self.ring           = RingBuffer(sample_rate, buffer_seconds, channels)
        self._audio_q       = queue.Queue(maxsize=queue_depth)
        self._stop_event    = threading.Event()
        self._recording     = False
        self._record_lock   = threading.Lock()
        self._post_deadline = None
        self._rec_start     = None
        self._rec_frames: list[np.ndarray] = []
        self._stream        = None
        self._worker        = None
        self._status_thread = None

        # PyAudio (imported lazily)
        self._pa = None

        # Dirs
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Logging
        logging.basicConfig(
            level=getattr(logging, log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s [specter-rx] %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
                self._build_file_handler(),
            ],
        )
        self.log = logging.getLogger("specter.rx_ring_buffer")

        # MQTT
        self._mqtt: MQTTClient | None = None
        if mqtt_enabled:
            self._mqtt = MQTTClient(mqtt_broker, mqtt_port,
                                    trigger_cb=self.trigger,
                                    log=self.log)

    # ── Logging helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _build_file_handler() -> logging.Handler:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(LOG_DIR / "rx_ring_buffer.log")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [specter-rx] %(message)s"))
            return fh
        except PermissionError:
            return logging.NullHandler()

    # ── Device enumeration ─────────────────────────────────────────────────────

    def list_devices(self) -> None:
        pa = self._get_pa()
        print(f"\n{'Idx':>4}  {'Name':<40}  {'In':>3}  {'Out':>3}  {'Rate':>8}")
        print("─" * 65)
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            print(f"{i:>4}  {info.get('name', ''):<40}"
                  f"  {int(info.get('maxInputChannels',0)):>3}"
                  f"  {int(info.get('maxOutputChannels',0)):>3}"
                  f"  {int(info.get('defaultSampleRate',0)):>8}")
        print()

    # ── PyAudio lazy init ──────────────────────────────────────────────────────

    def _get_pa(self):
        if self._pa is None:
            pyaudio = _require("pyaudio")
            self._pa = pyaudio.PyAudio()
        return self._pa

    # ── Audio callback ─────────────────────────────────────────────────────────

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudio
        if status:
            self.log.warning("Audio callback status: %s", status)
        try:
            audio = np.frombuffer(in_data, dtype=np.int16).copy()
            self._audio_q.put_nowait(audio)
        except queue.Full:
            self._queue_overflow_count += 1
            if self._queue_overflow_count % 100 == 1:
                self.log.warning(
                    "Audio queue overflow #%d — consider increasing --queue-depth (currently %d)",
                    self._queue_overflow_count, self.queue_depth,
                )
        return (None, pyaudio.paContinue)

    # ── VOX level ──────────────────────────────────────────────────────────────

    @staticmethod
    def _audio_level(chunk: np.ndarray) -> float:
        if len(chunk) == 0:
            return 0.0
        f = chunk.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(f * f)))

    # ── Trigger ────────────────────────────────────────────────────────────────

    def trigger(self, reason: str = "manual") -> None:
        with self._record_lock:
            now = time.time()
            if not self._recording:
                self._recording     = True
                self._rec_start     = now
                self._rec_frames    = [self.ring.read_all()]
                self._post_deadline = now + self.post_seconds
                self._last_trigger_reason = reason
                self.log.info("Recording started — reason: %s", reason)
                self._mqtt_publish(TOPIC_RECORDING, "1")
            else:
                # Extend post-deadline (honour max cap)
                elapsed = now - (self._rec_start or now)
                remaining_budget = self.max_record_sec - elapsed
                extension = min(self.post_seconds, max(0, remaining_budget))
                self._post_deadline = max(
                    self._post_deadline or 0,
                    now + extension,
                )
                self.log.info("Recording extended — reason: %s (budget remaining: %.0fs)",
                              reason, remaining_budget)

    # ── Trigger file check ─────────────────────────────────────────────────────

    def _check_trigger_file(self) -> None:
        try:
            if self.trigger_file.exists():
                self.trigger_file.unlink(missing_ok=True)
                self.trigger("trigger-file")
        except OSError as exc:
            self.log.debug("Trigger file check error: %s", exc)

    # ── Max-duration guard ─────────────────────────────────────────────────────

    def _check_max_duration(self) -> bool:
        """Return True if the current recording has exceeded max_record_sec."""
        with self._record_lock:
            if self._recording and self._rec_start:
                if time.time() - self._rec_start >= self.max_record_sec:
                    return True
        return False

    # ── Finalize recording ─────────────────────────────────────────────────────

    def _finalize_recording(self, reason: str = "post-timeout") -> None:
        with self._record_lock:
            if not self._recording or not self._rec_frames:
                return
            self.log.info("Finalizing recording — reason: %s", reason)

            audio = (
                np.concatenate(self._rec_frames)
                if len(self._rec_frames) > 1
                else self._rec_frames[0]
            )
            if self.channels > 1:
                audio = audio.reshape(-1, self.channels)

            ts       = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            out_path = self.output_dir / f"rx_capture_{ts}.wav"
            tmp_path = out_path.with_suffix(".wav.part")

            try:
                sf = _require("soundfile")
                sf.write(str(tmp_path), audio, self.sample_rate,
                         subtype="PCM_16", format="WAV")
                os.replace(tmp_path, out_path)
                self.log.info("Saved: %s", out_path)
            except Exception as exc:
                self.log.error("Failed to write WAV: %s", exc)
                tmp_path.unlink(missing_ok=True)
                self._reset_recording_state()
                return

            # ── Sidecar JSON metadata ──────────────────────────────────────
            duration_sec = len(audio) / (self.sample_rate * self.channels)
            meta = {
                "version":        VERSION,
                "timestamp_utc":  f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T"
                                  f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z",
                "reason":         self._last_trigger_reason,
                "duration_sec":   round(duration_sec, 3),
                "sample_rate":    self.sample_rate,
                "channels":       self.channels,
                "buffer_seconds": self.buffer_seconds,
                "post_seconds":   self.post_seconds,
                "device_index":   self.device_index,
                "sdr_source":     self.sdr_source,
                "frequency_hz":   self.frequency_hz,
                "vox_threshold":  self.vox_threshold,
                "queue_overflows":self._queue_overflow_count,
                "capture_index":  self._capture_count + 1,
                "file":           out_path.name,
                "operator_notes": "",
            }
            json_path = out_path.with_suffix(".json")
            try:
                json_path.write_text(json.dumps(meta, indent=2))
            except Exception as exc:
                self.log.warning("Failed to write sidecar JSON: %s", exc)

            # ── Counters & MQTT ────────────────────────────────────────────
            self._capture_count      += 1
            self._last_capture_file   = out_path.name

            event_payload = {
                "file":      out_path.name,
                "reason":    self._last_trigger_reason,
                "timestamp": meta["timestamp_utc"],
                "duration":  meta["duration_sec"],
            }
            self._mqtt_publish(TOPIC_EVENT,     event_payload)
            self._mqtt_publish(TOPIC_RECORDING, "0")

            self._reset_recording_state()

    def _reset_recording_state(self) -> None:
        self._recording     = False
        self._rec_frames    = []
        self._post_deadline = None
        self._rec_start     = None

    # ── Process loop ───────────────────────────────────────────────────────────

    def _process_loop(self) -> None:
        self.log.info("Process loop started")
        while not self._stop_event.is_set():
            self._check_trigger_file()

            # Max duration guard
            if self._check_max_duration():
                self.log.warning("Max recording duration (%ds) reached — forcing finalize",
                                 self.max_record_sec)
                self._finalize_recording(reason="max-duration")
                self._mqtt_publish(TOPIC_ALARM,
                    json.dumps({"source": "specter-rx",
                                "msg": "Recording truncated at max duration"}))

            try:
                chunk = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                with self._record_lock:
                    if (self._recording
                            and self._post_deadline
                            and time.time() >= self._post_deadline):
                        self._finalize_recording()
                continue

            # Feed ring buffer
            self.ring.write(chunk)

            # VOX detection
            if self.vox_threshold is not None:
                level = self._audio_level(chunk)
                self._vox_level = level
                if level >= self.vox_threshold:
                    self.trigger(f"vox level={level:.4f}")

            # Accumulate if recording
            with self._record_lock:
                if self._recording:
                    self._rec_frames.append(chunk)

            # Post-trigger timeout check
            with self._record_lock:
                if (self._recording
                        and self._post_deadline
                        and time.time() >= self._post_deadline):
                    pass  # finalized below outside lock
            if (not self._recording is False):
                with self._record_lock:
                    timed_out = (
                        self._recording
                        and self._post_deadline
                        and time.time() >= self._post_deadline
                    )
                if timed_out:
                    self._finalize_recording()

        # Drain on shutdown
        if self._recording:
            self._finalize_recording(reason="service-shutdown")
        self.log.info("Process loop stopped")

    # ── MQTT status heartbeat ──────────────────────────────────────────────────

    def _status_loop(self) -> None:
        while not self._stop_event.is_set():
            uptime = int(time.time() - self._service_start_time)
            payload = {
                "service":          "specter_rx_ring_buffer",
                "version":          VERSION,
                "uptime_sec":       uptime,
                "recording":        self._recording,
                "capture_count":    self._capture_count,
                "last_file":        self._last_capture_file,
                "last_trigger":     self._last_trigger_reason,
                "vox_level":        round(self._vox_level, 4),
                "queue_overflows":  self._queue_overflow_count,
                "queue_depth":      self.queue_depth,
                "device_index":     self.device_index,
                "sdr_source":       self.sdr_source,
                "frequency_hz":     self.frequency_hz,
                "mqtt_connected":   self._mqtt.connected if self._mqtt else False,
                "timestamp_utc":    dt.datetime.utcnow().isoformat() + "Z",
            }
            self._mqtt_publish(TOPIC_STATUS, payload)
            self._stop_event.wait(MQTT_STATUS_INTERVAL)

    def _mqtt_publish(self, topic: str, payload) -> None:
        if self._mqtt:
            self._mqtt.publish(topic, payload)

    # ── Start / Stop ───────────────────────────────────────────────────────────

    def start(self) -> None:
        import pyaudio
        pa = self._get_pa()
        self.log.info(
            "Starting SPECTER RX Rolling Capture: "
            "rate=%d  channels=%d  buffer=%ds  post=%ds  max=%ds  "
            "chunk=%d  queue_depth=%d  device=%s",
            self.sample_rate, self.channels, self.buffer_seconds,
            self.post_seconds, self.max_record_sec,
            self.chunk_size, self.queue_depth, self.device_index,
        )
        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=self.chunk_size,
            stream_callback=self._callback,
            start=False,
        )
        self._stream.start_stream()

        self._worker = threading.Thread(
            target=self._process_loop, name="specter-rx-process", daemon=True)
        self._worker.start()

        self._status_thread = threading.Thread(
            target=self._status_loop, name="specter-rx-mqtt-status", daemon=True)
        self._status_thread.start()

        self.log.info("SPECTER RX service running. Trigger: %s", self.trigger_file)

    def stop(self) -> None:
        self.log.info("Stopping SPECTER RX service")
        self._stop_event.set()
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        finally:
            if self._pa:
                self._pa.terminate()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)
        if self._mqtt:
            self._mqtt.stop()


# ─── Installer ────────────────────────────────────────────────────────────────

def install(args) -> int:
    """
    Install SPECTER RX Ring Buffer as a systemd service.
    Must be run as root (or via sudo).
    """
    import shutil

    print("\n╔══════════════════════════════════════════════╗")
    print("║   SPECTER RX Ring Buffer — Installer         ║")
    print("╚══════════════════════════════════════════════╝\n")

    if os.geteuid() != 0:
        print("[ERROR] Install must be run as root: sudo python3 specter_rx_ring_buffer.py install")
        return 1

    # Directories
    for d in [APP_DIR, CONFIG_DIR, LOG_DIR, RECORD_DIR]:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  [DIR]  {d}")

    # Service user
    result = subprocess.run(["id", DEFAULT_USER], capture_output=True)
    if result.returncode != 0:
        subprocess.run(["useradd", "-r", "-s", "/bin/false",
                        "-G", "audio", DEFAULT_USER], check=True)
        print(f"  [USER] Created system user: {DEFAULT_USER}")
    else:
        print(f"  [USER] User exists: {DEFAULT_USER}")

    # Ownership
    for d in [APP_DIR, LOG_DIR, RECORD_DIR]:
        subprocess.run(["chown", "-R", f"{DEFAULT_USER}:{DEFAULT_USER}", str(d)], check=True)

    # Copy script
    script_dest = APP_DIR / "specter_rx_ring_buffer.py"
    shutil.copy2(__file__, script_dest)
    os.chmod(script_dest, 0o755)
    print(f"  [COPY] {__file__} → {script_dest}")

    # Venv
    if not (VENV_DIR / "bin" / "python").exists():
        print(f"  [VENV] Creating {VENV_DIR} ...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    pip = VENV_DIR / "bin" / "pip"
    print("  [PIP]  Installing dependencies ...")
    subprocess.run([str(pip), "install", "--upgrade", "pip",
                    "numpy", "soundfile", "pyaudio", "paho-mqtt"], check=True)
    venv_python = VENV_DIR / "bin" / "python"

    # systemd service file
    service_content = SYSTEMD_SERVICE_TEMPLATE.format(
        user=DEFAULT_USER,
        app_dir=APP_DIR,
        venv_python=venv_python,
        script_path=script_dest,
        record_dir=RECORD_DIR,
        trigger_file=TRIGGER_FILE,
        mqtt_broker=args.mqtt_broker,
    )
    service_path = Path(f"/etc/systemd/system/{SERVICE_NAME}")
    service_path.write_text(service_content)
    print(f"  [UNIT] {service_path}")

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
    subprocess.run(["systemctl", "start",  SERVICE_NAME], check=True)
    print(f"\n  [OK]   {SERVICE_NAME} enabled and started")

    # Status
    time.sleep(2)
    subprocess.run(["systemctl", "status", SERVICE_NAME, "--no-pager", "-l"])

    print("\n  Install complete. Tail logs with:")
    print(f"    journalctl -u {SERVICE_NAME} -f\n")
    return 0


# ─── Trigger helper ───────────────────────────────────────────────────────────

def send_trigger(args) -> int:
    """Touch the trigger file so a running service captures an event."""
    trigger_path = Path(args.trigger_file)
    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    trigger_path.touch()
    print(f"[SPECTER] Trigger written: {trigger_path}")
    return 0


# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specter_rx_ring_buffer",
        description="SPECTER RX Rolling Capture Service v" + VERSION,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── run ────────────────────────────────────────────────────────────────────
    run_p = sub.add_parser("run", help="Start the capture service")
    run_p.add_argument("--device-index",   type=int,   default=None)
    run_p.add_argument("--sample-rate",    type=int,   default=DEFAULT_SAMPLE_RATE)
    run_p.add_argument("--channels",       type=int,   default=DEFAULT_CHANNELS)
    run_p.add_argument("--buffer-seconds", type=int,   default=DEFAULT_BUFFER_SECONDS,
                       help="Pre-trigger rolling window (seconds)")
    run_p.add_argument("--post-seconds",   type=int,   default=DEFAULT_POST_SECONDS,
                       help="Post-trigger capture duration (seconds)")
    run_p.add_argument("--max-record-seconds", type=int, default=DEFAULT_MAX_RECORD_SEC,
                       help="Hard cap on recording length (seconds)")
    run_p.add_argument("--chunk-size",     type=int,   default=DEFAULT_CHUNK_SIZE)
    run_p.add_argument("--queue-depth",    type=int,   default=DEFAULT_QUEUE_DEPTH,
                       help="Audio queue depth (chunks). Increase if overflows occur.")
    run_p.add_argument("--output-dir",     default=str(RECORD_DIR))
    run_p.add_argument("--trigger-file",   default=str(TRIGGER_FILE))
    run_p.add_argument("--vox-threshold",  type=float, default=None,
                       help="RMS VOX threshold 0.0–1.0 (disabled if omitted)")
    run_p.add_argument("--mqtt-broker",    default=MQTT_BROKER_DEFAULT)
    run_p.add_argument("--mqtt-port",      type=int,   default=MQTT_PORT_DEFAULT)
    run_p.add_argument("--no-mqtt",        action="store_true",
                       help="Disable MQTT entirely")
    run_p.add_argument("--sdr-source",     default="audio-input",
                       help="Label for metadata (e.g. hackrf, pluto, rtlsdr-v4)")
    run_p.add_argument("--frequency-hz",   type=int,   default=None,
                       help="Center frequency for metadata (Hz)")
    run_p.add_argument("--log-level",      default="INFO",
                       choices=["DEBUG","INFO","WARNING","ERROR"])

    # ── install ────────────────────────────────────────────────────────────────
    inst_p = sub.add_parser("install", help="Install as systemd service (requires root)")
    inst_p.add_argument("--mqtt-broker", default=MQTT_BROKER_DEFAULT)

    # ── trigger ────────────────────────────────────────────────────────────────
    trig_p = sub.add_parser("trigger", help="Send file trigger to running service")
    trig_p.add_argument("--trigger-file", default=str(TRIGGER_FILE))

    # ── list-devices ───────────────────────────────────────────────────────────
    sub.add_parser("list-devices", help="List available audio input devices")

    return parser


def main() -> int:
    parser = build_parser()
    args   = parser.parse_args()

    if args.command == "install":
        return install(args)

    if args.command == "trigger":
        return send_trigger(args)

    if args.command == "list-devices":
        svc = RXBufferService(mqtt_enabled=False)
        svc.list_devices()
        return 0

    # ── run ────────────────────────────────────────────────────────────────────
    svc = RXBufferService(
        device_index    = args.device_index,
        sample_rate     = args.sample_rate,
        channels        = args.channels,
        buffer_seconds  = args.buffer_seconds,
        post_seconds    = args.post_seconds,
        max_record_sec  = args.max_record_seconds,
        chunk_size      = args.chunk_size,
        queue_depth     = args.queue_depth,
        output_dir      = args.output_dir,
        trigger_file    = args.trigger_file,
        vox_threshold   = args.vox_threshold,
        mqtt_broker     = args.mqtt_broker,
        mqtt_port       = args.mqtt_port,
        mqtt_enabled    = not args.no_mqtt,
        sdr_source      = args.sdr_source,
        frequency_hz    = args.frequency_hz,
        log_level       = args.log_level,
    )

    def _handle_signal(signum, frame):
        svc.log.info("Signal %s received — shutting down", signum)
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    svc.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
