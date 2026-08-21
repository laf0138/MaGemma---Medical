#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              SPECTER — AUDIO RECORDER UTILITY  (audio_recorder.py)          ║
║                                                                              ║
║  Standalone timed audio capture utility.                                    ║
║  Distinct from the ring buffer — this records a fixed-length clip on demand.║
║  Used by operators to manually capture audio from any audio device.         ║
║                                                                              ║
║  Usage:                                                                     ║
║    python3 audio_recorder.py --duration 30 --output /mnt/specter/live/      ║
║    python3 audio_recorder.py --list-devices                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-audio] %(message)s",
)
log = logging.getLogger("specter.audio_recorder")

RECORD_DIR = Path("/mnt/specter/live/recordings")
DEFAULT_RATE     = 48_000
DEFAULT_CHANNELS = 1
DEFAULT_DURATION = 30


def list_devices():
    import pyaudio
    pa = pyaudio.PyAudio()
    print(f"\n{'Idx':>4}  {'Name':<40}  {'In':>3}  {'Rate':>8}")
    print("─" * 60)
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            print(f"{i:>4}  {info.get('name',''):<40}"
                  f"  {int(info.get('maxInputChannels',0)):>3}"
                  f"  {int(info.get('defaultSampleRate',0)):>8}")
    pa.terminate()
    print()


def record(device_index: int | None, sample_rate: int, channels: int,
           duration: int, output_dir: Path, label: str = "manual") -> Path:
    import pyaudio
    import soundfile as sf

    output_dir.mkdir(parents=True, exist_ok=True)
    chunk = 4096
    frames = []

    pa     = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=sample_rate,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=chunk,
    )

    log.info("Recording for %ds on device=%s ...", duration, device_index)
    end_time = time.time() + duration
    while time.time() < end_time:
        data  = stream.read(chunk, exception_on_overflow=False)
        audio = np.frombuffer(data, dtype=np.int16)
        frames.append(audio)

    stream.stop_stream()
    stream.close()
    pa.terminate()

    audio_full = np.concatenate(frames)
    if channels > 1:
        audio_full = audio_full.reshape(-1, channels)

    ts       = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = output_dir / f"audio_{label}_{ts}.wav"
    tmp_path = out_path.with_suffix(".wav.part")

    sf.write(str(tmp_path), audio_full, sample_rate, subtype="PCM_16")
    import os; os.replace(tmp_path, out_path)

    meta = {
        "timestamp_utc": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z",
        "label":         label,
        "duration_sec":  duration,
        "sample_rate":   sample_rate,
        "channels":      channels,
        "device_index":  device_index,
        "file":          out_path.name,
    }
    out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))

    log.info("Saved: %s", out_path)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="SPECTER Audio Recorder Utility")
    parser.add_argument("--device-index", type=int, default=None)
    parser.add_argument("--sample-rate",  type=int, default=DEFAULT_RATE)
    parser.add_argument("--channels",     type=int, default=DEFAULT_CHANNELS)
    parser.add_argument("--duration",     type=int, default=DEFAULT_DURATION, help="Seconds to record")
    parser.add_argument("--output",       default=str(RECORD_DIR))
    parser.add_argument("--label",        default="manual", help="Label for filename")
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return 0

    record(
        device_index = args.device_index,
        sample_rate  = args.sample_rate,
        channels     = args.channels,
        duration     = args.duration,
        output_dir   = Path(args.output),
        label        = args.label,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
