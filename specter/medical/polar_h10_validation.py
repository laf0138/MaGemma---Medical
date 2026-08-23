#!/usr/bin/env python3
"""Polar H10 collection and validation harness.

The offline self-test verifies SPECTER's capture, timestamp reconstruction,
continuity checks, ECG-analysis handoff, artifact export, and failure labels.
The live mode uses the same path with a physical H10 but never publishes MQTT
data and never changes the production device-verification gate.

Passing this harness is an integration result, not clinical validation. A
simultaneously recorded diagnostic ECG and qualified review are still required
to evaluate measurement accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import math
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import neurokit2 as nk
from bleak import BleakClient, BleakScanner
from bleakheart import HeartRate, PolarMeasurementData

try:
    from medical.ecg_analysis import analyze_ecg_waveform
except ImportError:
    from ecg_analysis import analyze_ecg_waveform


SAMPLE_RATE_HZ = 130
MIN_CAPTURE_SECONDS = 10.0
MIN_SAMPLE_COVERAGE = 0.98
MIN_REQUESTED_DURATION_COVERAGE = 0.90
SCHEMA_VERSION = 1
NOT_CLINICALLY_VALIDATED = "NOT_EVALUATED_REQUIRES_REFERENCE_ECG"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EcgFrame:
    """A bleakheart ECG queue item; timestamp refers to the last sample."""

    timestamp_ns: int
    samples_uv: list[int]


@dataclass
class HeartRateFrame:
    timestamp_ns: int
    average_bpm: int
    rr_intervals_ms: list[int]


@dataclass
class Capture:
    provenance: str
    requested_duration_s: float
    started_at_utc: str
    device_name: str
    device_identifier: str
    ecg_frames: list[EcgFrame] = field(default_factory=list)
    heart_rate_frames: list[HeartRateFrame] = field(default_factory=list)
    stream_start_succeeded: bool = False
    stream_stop_succeeded: bool = False
    hr_notify_start_succeeded: bool = False
    hr_notify_stop_succeeded: bool = False
    capture_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Capture":
        return cls(
            provenance=value["provenance"],
            requested_duration_s=float(value["requested_duration_s"]),
            started_at_utc=value["started_at_utc"],
            device_name=value["device_name"],
            device_identifier=value["device_identifier"],
            ecg_frames=[EcgFrame(**frame) for frame in value.get("ecg_frames", [])],
            heart_rate_frames=[
                HeartRateFrame(**frame)
                for frame in value.get("heart_rate_frames", [])
            ],
            stream_start_succeeded=bool(value.get("stream_start_succeeded")),
            stream_stop_succeeded=bool(value.get("stream_stop_succeeded")),
            hr_notify_start_succeeded=bool(value.get("hr_notify_start_succeeded")),
            hr_notify_stop_succeeded=bool(value.get("hr_notify_stop_succeeded")),
            capture_errors=list(value.get("capture_errors", [])),
        )


def reconstruct_samples(
    frames: Sequence[EcgFrame], sampling_rate_hz: int = SAMPLE_RATE_HZ
) -> list[dict[str, int]]:
    """Expand frame timestamps into one timestamped row per ECG sample."""
    if sampling_rate_hz <= 0:
        raise ValueError("sampling_rate_hz must be positive")
    period_ns = 1_000_000_000 / sampling_rate_hz
    rows: list[dict[str, int]] = []
    sample_index = 0
    for frame_index, frame in enumerate(frames):
        if not isinstance(frame.timestamp_ns, int) or frame.timestamp_ns < 0:
            raise ValueError(f"frame {frame_index} has an invalid timestamp")
        if not frame.samples_uv:
            raise ValueError(f"frame {frame_index} has no ECG samples")
        first_timestamp = frame.timestamp_ns - round(
            (len(frame.samples_uv) - 1) * period_ns
        )
        for offset, sample in enumerate(frame.samples_uv):
            if isinstance(sample, bool) or not isinstance(sample, int):
                raise ValueError(f"frame {frame_index} contains a non-integer sample")
            rows.append(
                {
                    "sample_index": sample_index,
                    "frame_index": frame_index,
                    "timestamp_ns": first_timestamp + round(offset * period_ns),
                    "sample_uv": sample,
                }
            )
            sample_index += 1
    return rows


def validate_capture(capture: Capture) -> dict[str, Any]:
    """Validate transport integrity without claiming physiologic accuracy."""
    errors = list(capture.capture_errors)
    warnings: list[str] = []
    rows: list[dict[str, int]] = []
    try:
        rows = reconstruct_samples(capture.ecg_frames)
    except ValueError as exc:
        errors.append(str(exc))

    if not capture.stream_start_succeeded:
        errors.append("ECG stream did not start successfully")
    if not capture.stream_stop_succeeded:
        errors.append("ECG stream was not confirmed stopped")
    if capture.provenance == "live_hardware":
        if not capture.hr_notify_start_succeeded:
            errors.append("heart-rate notifications did not start successfully")
        if not capture.hr_notify_stop_succeeded:
            errors.append("heart-rate notifications were not confirmed stopped")
    if len(capture.ecg_frames) < 2:
        errors.append("fewer than two ECG frames were captured")

    timestamp_regressions = 0
    missing_sample_estimate = 0
    coverage_ratio = 0.0
    duration_s = 0.0
    if rows:
        timestamps = [row["timestamp_ns"] for row in rows]
        timestamp_regressions = sum(
            current <= previous
            for previous, current in zip(timestamps, timestamps[1:])
        )
        if timestamp_regressions:
            errors.append(
                f"{timestamp_regressions} duplicate or decreasing sample timestamp(s)"
            )

        if len(rows) > 1 and timestamps[-1] > timestamps[0]:
            duration_s = (timestamps[-1] - timestamps[0]) / 1_000_000_000
            expected_samples = round(duration_s * SAMPLE_RATE_HZ) + 1
            coverage_ratio = min(1.0, len(rows) / expected_samples)
            missing_sample_estimate = max(0, expected_samples - len(rows))
        elif len(rows) == 1:
            coverage_ratio = 1.0

    sample_duration_s = len(rows) / SAMPLE_RATE_HZ
    minimum_duration_s = max(
        MIN_CAPTURE_SECONDS,
        capture.requested_duration_s * MIN_REQUESTED_DURATION_COVERAGE,
    )
    if sample_duration_s < minimum_duration_s:
        errors.append(
            f"captured {sample_duration_s:.2f}s of ECG; at least "
            f"{minimum_duration_s:.2f}s required"
        )
    if rows and coverage_ratio < MIN_SAMPLE_COVERAGE:
        errors.append(
            f"sample coverage {coverage_ratio:.3f} is below {MIN_SAMPLE_COVERAGE:.2f}"
        )

    hr_values = [frame.average_bpm for frame in capture.heart_rate_frames]
    rr_values = [
        rr
        for frame in capture.heart_rate_frames
        for rr in frame.rr_intervals_ms
    ]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in hr_values
    ):
        errors.append("heart-rate frames contain a non-positive or non-integer BPM")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in rr_values
    ):
        errors.append("heart-rate frames contain a non-positive or non-integer RR interval")
    if not hr_values:
        message = "no heart-rate notification frames were captured"
        if capture.provenance == "live_hardware":
            errors.append(message)
        else:
            warnings.append(message)

    analysis = analyze_ecg_waveform(
        [row["sample_uv"] for row in rows], sampling_rate=SAMPLE_RATE_HZ
    ) if rows else {"error": "No ECG samples available"}

    offline_verified = capture.provenance in {"synthetic", "replay"}
    hardware_status = (
        "PASS_INTEGRATION_ONLY" if capture.provenance == "live_hardware" and not errors
        else "FAIL" if capture.provenance == "live_hardware"
        else "NOT_RUN_NO_HARDWARE"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": not errors,
        "provenance": capture.provenance,
        "offline_harness_verified": offline_verified and not errors,
        "hardware_integration_status": hardware_status,
        "clinical_accuracy_status": NOT_CLINICALLY_VALIDATED,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "ecg_frame_count": len(capture.ecg_frames),
            "ecg_sample_count": len(rows),
            "ecg_sample_duration_seconds": round(sample_duration_s, 3),
            "capture_span_seconds": round(duration_s, 3),
            "sample_coverage_ratio": round(coverage_ratio, 5),
            "estimated_missing_samples": missing_sample_estimate,
            "timestamp_regressions": timestamp_regressions,
            "heart_rate_frame_count": len(capture.heart_rate_frames),
            "rr_interval_count": len(rr_values),
        },
        "ecg_analysis": analysis,
        "interpretation": (
            "PASS verifies collection-path integrity only. It does not establish "
            "diagnostic or clinical measurement accuracy."
        ),
    }


def synthetic_capture(duration_s: float = 15.0) -> Capture:
    """Create deterministic 130Hz frames shaped like bleakheart output."""
    if not math.isfinite(duration_s) or duration_s < MIN_CAPTURE_SECONDS:
        raise ValueError(f"duration must be at least {MIN_CAPTURE_SECONDS:.0f} seconds")
    signal_mv = nk.ecg_simulate(
        duration=duration_s,
        sampling_rate=SAMPLE_RATE_HZ,
        heart_rate=72,
        noise=0.01,
        random_state=42,
    )
    samples = [int(round(value * 1000)) for value in signal_mv]
    base_ns = 1_800_000_000_000_000_000
    period_ns = 1_000_000_000 / SAMPLE_RATE_HZ
    frame_size = 73
    frames = []
    for start in range(0, len(samples), frame_size):
        payload = samples[start : start + frame_size]
        last_index = start + len(payload) - 1
        frames.append(
            EcgFrame(
                timestamp_ns=base_ns + round(last_index * period_ns),
                samples_uv=payload,
            )
        )
    heart_rate_frames = [
        HeartRateFrame(
            timestamp_ns=base_ns + second * 1_000_000_000,
            average_bpm=72,
            rr_intervals_ms=[833],
        )
        for second in range(1, int(duration_s))
    ]
    return Capture(
        provenance="synthetic",
        requested_duration_s=duration_s,
        started_at_utc=_utc_now(),
        device_name="SYNTHETIC Polar H10",
        device_identifier="SIMULATED-NO-HARDWARE",
        ecg_frames=frames,
        heart_rate_frames=heart_rate_frames,
        stream_start_succeeded=True,
        stream_stop_succeeded=True,
        hr_notify_start_succeeded=True,
        hr_notify_stop_succeeded=True,
    )


def _device_name(device: Any, advertisement: Any = None) -> str:
    return (
        getattr(device, "name", None)
        or getattr(advertisement, "local_name", None)
        or getattr(device, "address", "unknown")
    )


async def find_polar_h10(
    selector: str | None = None,
    scan_timeout_s: float = 10.0,
    scanner: Any = BleakScanner,
) -> Any:
    """Return one explicitly selected H10, rejecting ambiguous scans."""
    discovered = await scanner.discover(timeout=scan_timeout_s, return_adv=True)
    candidates = []
    items: Iterable[Any] = (
        discovered.values() if isinstance(discovered, dict) else discovered
    )
    for item in items:
        if isinstance(item, tuple):
            device, advertisement = item
        else:
            device, advertisement = item, None
        name = _device_name(device, advertisement)
        address = str(getattr(device, "address", ""))
        if "polar h10" not in name.lower():
            continue
        if (
            selector
            and selector.lower() not in name.lower()
            and selector.lower() not in address.lower()
        ):
            continue
        candidates.append(device)
    if not candidates:
        raise RuntimeError("No matching Polar H10 found")
    if len(candidates) > 1 and not selector:
        names = ", ".join(_device_name(device) for device in candidates)
        raise RuntimeError(f"Multiple Polar H10 devices found; use --device: {names}")
    return candidates[0]


async def capture_live(
    duration_s: float,
    selector: str | None = None,
    scan_timeout_s: float = 10.0,
    *,
    scanner: Any = BleakScanner,
    client_factory: Any = BleakClient,
    pmd_factory: Any = PolarMeasurementData,
    hr_factory: Any = HeartRate,
) -> Capture:
    """Capture an H10 without publishing or changing the production gate."""
    if not math.isfinite(duration_s) or duration_s < MIN_CAPTURE_SECONDS:
        raise ValueError(f"duration must be at least {MIN_CAPTURE_SECONDS:.0f} seconds")
    device = await find_polar_h10(selector, scan_timeout_s, scanner=scanner)
    capture = Capture(
        provenance="live_hardware",
        requested_duration_s=duration_s,
        started_at_utc=_utc_now(),
        device_name=_device_name(device),
        device_identifier=str(getattr(device, "address", "unknown")),
    )
    ecg_queue: asyncio.Queue = asyncio.Queue()
    hr_queue: asyncio.Queue = asyncio.Queue()

    client_context = client_factory(device)
    try:
        client = await client_context.__aenter__()
    except Exception as exc:
        capture.capture_errors.append(f"Bluetooth connection failed: {exc}")
        return capture

    pmd = None
    hr = None
    try:
        pmd = pmd_factory(client, ecg_queue=ecg_queue)
        hr = hr_factory(client, queue=hr_queue, unpack=False)
        start_result = await pmd.start_streaming("ECG")
        if not start_result or start_result[0] != 0:
            message = start_result[1] if len(start_result) > 1 else "unknown error"
            capture.capture_errors.append(f"ECG stream start failed: {message}")
            return capture
        capture.stream_start_succeeded = True
        await hr.start_notify()
        capture.hr_notify_start_succeeded = True

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                dtype, timestamp_ns, payload = await asyncio.wait_for(
                    ecg_queue.get(), timeout=min(0.25, remaining)
                )
                if dtype == "ECG":
                    capture.ecg_frames.append(
                        EcgFrame(int(timestamp_ns), list(payload))
                    )
            except asyncio.TimeoutError:
                pass
            while not hr_queue.empty():
                (
                    _dtype,
                    timestamp_ns,
                    (average_bpm, rr_list),
                    _energy,
                ) = hr_queue.get_nowait()
                capture.heart_rate_frames.append(
                    HeartRateFrame(
                        int(timestamp_ns), int(average_bpm), list(rr_list)
                    )
                )
        # An HR notification may have arrived after the final ECG queue
        # read but before the deadline. Preserve it rather than silently
        # leaving the last frame behind.
        while not hr_queue.empty():
            (
                _dtype,
                timestamp_ns,
                (average_bpm, rr_list),
                _energy,
            ) = hr_queue.get_nowait()
            capture.heart_rate_frames.append(
                HeartRateFrame(int(timestamp_ns), int(average_bpm), list(rr_list))
            )
    except Exception as exc:
        capture.capture_errors.append(f"live capture failed: {exc}")
    finally:
        if capture.hr_notify_start_succeeded and hr is not None:
            try:
                await hr.stop_notify()
                capture.hr_notify_stop_succeeded = True
            except Exception as exc:
                capture.capture_errors.append(
                    f"heart-rate notification stop failed: {exc}"
                )
        if capture.stream_start_succeeded and pmd is not None:
            try:
                stop_result = await pmd.stop_streaming("ECG")
                if stop_result and stop_result[0] == 0:
                    capture.stream_stop_succeeded = True
                else:
                    message = (
                        stop_result[1]
                        if stop_result and len(stop_result) > 1
                        else "unknown error"
                    )
                    capture.capture_errors.append(f"ECG stream stop failed: {message}")
            except Exception as exc:
                capture.capture_errors.append(f"ECG stream stop failed: {exc}")
        try:
            await client_context.__aexit__(None, None, None)
        except Exception as exc:
            capture.capture_errors.append(f"Bluetooth disconnect failed: {exc}")
    return capture


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def write_artifacts(
    output_dir: Path, capture: Capture, report: dict[str, Any]
) -> Path:
    """Write auditable JSON/CSV artifacts and hashes; never overwrite a run."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        rows = reconstruct_samples(capture.ecg_frames) if capture.ecg_frames else []
    except ValueError:
        # A malformed capture is exactly the evidence a failed live run needs
        # to preserve. report.json contains the validation error; keep the raw
        # frame data in capture.json and emit a header-only sample CSV.
        rows = []
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": _utc_now(),
        "provenance": capture.provenance,
        "device_name": capture.device_name,
        "device_identifier": capture.device_identifier,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {
            "bleak": _version("bleak"),
            "bleakheart": _version("bleakheart"),
            "neurokit2": _version("neurokit2"),
        },
        "hardware_integration_status": report["hardware_integration_status"],
        "clinical_accuracy_status": report["clinical_accuracy_status"],
    }

    (output_dir / "capture.json").write_text(
        json.dumps(capture.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    with (output_dir / "ecg_samples.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sample_index", "frame_index", "timestamp_ns", "sample_uv"],
        )
        writer.writeheader()
        writer.writerows(rows)
    with (output_dir / "heart_rate.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["frame_index", "timestamp_ns", "average_bpm", "rr_intervals_ms"]
        )
        for index, frame in enumerate(capture.heart_rate_frames):
            writer.writerow(
                [
                    index,
                    frame.timestamp_ns,
                    frame.average_bpm,
                    "|".join(map(str, frame.rr_intervals_ms)),
                ]
            )

    hashed_files = sorted(path for path in output_dir.iterdir() if path.is_file())
    checksum_lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in hashed_files
    ]
    (output_dir / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n")
    return output_dir


def read_capture(path: Path) -> Capture:
    capture_path = path / "capture.json" if path.is_dir() else path
    return Capture.from_dict(json.loads(capture_path.read_text()))


def default_output_dir(prefix: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"{prefix}-{timestamp}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline-verifiable Polar H10 collection validation harness"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true", help="run without hardware")
    mode.add_argument(
        "--live", action="store_true", help="capture a physical Polar H10"
    )
    mode.add_argument(
        "--replay", type=Path, help="revalidate capture.json or its directory"
    )
    parser.add_argument(
        "--duration", type=float, default=15.0, help="capture duration in seconds"
    )
    parser.add_argument(
        "--device", help="H10 name or address substring; required if scan is ambiguous"
    )
    parser.add_argument("--scan-timeout", type=float, default=10.0, help="Bluetooth scan timeout")
    parser.add_argument("--output", type=Path, help="new directory for validation artifacts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.self_test:
            capture = synthetic_capture(args.duration)
            prefix = "polar-h10-self-test"
        elif args.replay:
            capture = read_capture(args.replay)
            capture.provenance = "replay"
            prefix = "polar-h10-replay"
        else:
            capture = asyncio.run(
                capture_live(
                    duration_s=args.duration,
                    selector=args.device,
                    scan_timeout_s=args.scan_timeout,
                )
            )
            prefix = "polar-h10-live"

        report = validate_capture(capture)
        output = write_artifacts(
            args.output or default_output_dir(prefix), capture, report
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({
        "passed": report["passed"],
        "hardware_integration_status": report["hardware_integration_status"],
        "clinical_accuracy_status": report["clinical_accuracy_status"],
        "output": str(output),
    }, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
