"""Offline verification for the Polar H10 validation harness."""

import asyncio
import hashlib
from pathlib import Path

import pytest

import medical.polar_h10_validation as validation
from medical.polar_h10_validation import (
    Capture,
    EcgFrame,
    HeartRateFrame,
    capture_live,
    find_polar_h10,
    read_capture,
    reconstruct_samples,
    synthetic_capture,
    validate_capture,
    write_artifacts,
)


class FakeDevice:
    name = "Polar H10 TEST123"
    address = "AA:BB:CC:DD:EE:FF"


class FakeScanner:
    devices = {"address": (FakeDevice(), object())}

    @classmethod
    async def discover(cls, timeout, return_adv):
        assert return_adv is True
        return cls.devices


class FakeClient:
    def __init__(self, device):
        self.device = device

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakePMD:
    instances = []

    def __init__(self, client, ecg_queue=None, **kwargs):
        self.ecg_queue = ecg_queue
        self.stopped = False
        FakePMD.instances.append(self)

    async def start_streaming(self, measurement):
        assert measurement == "ECG"
        base = 1_800_000_000_000_000_000
        period = 1_000_000_000 / validation.SAMPLE_RATE_HZ
        for frame_index in range(4):
            last_sample_index = frame_index * 2 + 1
            await self.ecg_queue.put(
                (
                    "ECG",
                    base + round(last_sample_index * period),
                    [frame_index * 2, frame_index * 2 + 1],
                )
            )
        return 0, "SUCCESS", b""

    async def stop_streaming(self, measurement):
        assert measurement == "ECG"
        self.stopped = True
        return 0, "SUCCESS"


class FakeHeartRate:
    instances = []

    def __init__(self, client, queue=None, unpack=True, **kwargs):
        assert unpack is False
        self.queue = queue
        self.stopped = False
        FakeHeartRate.instances.append(self)

    async def start_notify(self):
        await self.queue.put(("HR", 123, (72, [833]), None))

    async def stop_notify(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakePMD.instances.clear()
    FakeHeartRate.instances.clear()


class TestOfflineSelfTest:
    def test_clean_synthetic_capture_passes_without_claiming_hardware(self):
        report = validate_capture(synthetic_capture())

        assert report["passed"] is True
        assert report["offline_harness_verified"] is True
        assert report["hardware_integration_status"] == "NOT_RUN_NO_HARDWARE"
        assert report["clinical_accuracy_status"].startswith("NOT_EVALUATED")
        assert report["metrics"]["sample_coverage_ratio"] >= 0.98
        assert report["ecg_analysis"]["analysis_status"] == (
            "experimental_not_clinically_validated"
        )

    def test_reconstruction_uses_last_sample_frame_timestamp(self):
        rows = reconstruct_samples([EcgFrame(timestamp_ns=1_000_000_000, samples_uv=[1, 2, 3])])
        assert rows[-1]["timestamp_ns"] == 1_000_000_000
        assert rows[-1]["sample_uv"] == 3
        assert rows[1]["timestamp_ns"] > rows[0]["timestamp_ns"]

    def test_missing_frame_fails_coverage(self):
        capture = synthetic_capture()
        del capture.ecg_frames[len(capture.ecg_frames) // 2]
        report = validate_capture(capture)
        assert report["passed"] is False
        assert report["metrics"]["estimated_missing_samples"] > 0
        assert any("sample coverage" in error for error in report["errors"])

    def test_decreasing_frame_timestamp_is_rejected(self):
        capture = synthetic_capture()
        capture.ecg_frames[2].timestamp_ns = capture.ecg_frames[0].timestamp_ns
        report = validate_capture(capture)
        assert report["passed"] is False
        assert report["metrics"]["timestamp_regressions"] > 0

    def test_capture_must_cover_requested_duration(self):
        capture = synthetic_capture()
        capture.requested_duration_s = 60
        report = validate_capture(capture)
        assert report["passed"] is False
        assert any("at least 54.00s required" in error for error in report["errors"])

    @pytest.mark.parametrize("bad_sample", [1.5, True, "2"])
    def test_non_integer_ecg_sample_is_rejected(self, bad_sample):
        capture = synthetic_capture()
        capture.ecg_frames[0].samples_uv[0] = bad_sample
        report = validate_capture(capture)
        assert report["passed"] is False
        assert any("non-integer sample" in error for error in report["errors"])

    def test_bad_hr_and_rr_values_are_rejected(self):
        capture = synthetic_capture()
        capture.heart_rate_frames = [HeartRateFrame(1, 0, [-1])]
        report = validate_capture(capture)
        assert report["passed"] is False
        assert any("BPM" in error for error in report["errors"])
        assert any("RR interval" in error for error in report["errors"])


class TestArtifacts:
    def test_artifacts_round_trip_and_checksums_verify(self, tmp_path):
        capture = synthetic_capture()
        report = validate_capture(capture)
        output = write_artifacts(tmp_path / "run", capture, report)

        assert read_capture(output).to_dict() == capture.to_dict()
        checksum_lines = (output / "SHA256SUMS").read_text().splitlines()
        assert len(checksum_lines) == 5
        for line in checksum_lines:
            expected, filename = line.split("  ", 1)
            assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == expected
        assert "NOT_RUN_NO_HARDWARE" in (output / "manifest.json").read_text()
        assert "NOT_EVALUATED_REQUIRES_REFERENCE_ECG" in (output / "report.json").read_text()

    def test_existing_output_directory_is_not_overwritten(self, tmp_path):
        output = tmp_path / "existing"
        output.mkdir()
        capture = synthetic_capture()
        with pytest.raises(FileExistsError):
            write_artifacts(output, capture, validate_capture(capture))

    def test_failed_malformed_capture_is_still_preserved(self, tmp_path):
        capture = synthetic_capture()
        capture.ecg_frames[0].samples_uv[0] = "bad"
        report = validate_capture(capture)
        output = write_artifacts(tmp_path / "failed-run", capture, report)

        assert report["passed"] is False
        assert "bad" in (output / "capture.json").read_text()
        assert len((output / "ecg_samples.csv").read_text().splitlines()) == 1

    def test_cli_self_test_writes_passing_report(self, tmp_path):
        output = tmp_path / "cli-run"
        return_code = validation.main(["--self-test", "--output", str(output)])
        assert return_code == 0
        assert (output / "report.json").exists()


class TestLivePreparation:
    def test_scan_rejects_ambiguous_h10_without_selector(self):
        second = FakeDevice()
        second.name = "Polar H10 OTHER"
        second.address = "11:22:33:44:55:66"

        class AmbiguousScanner(FakeScanner):
            devices = {
                "one": (FakeDevice(), object()),
                "two": (second, object()),
            }

        with pytest.raises(RuntimeError, match="Multiple Polar H10"):
            asyncio.run(find_polar_h10(scanner=AmbiguousScanner))
        selected = asyncio.run(
            find_polar_h10(selector="OTHER", scanner=AmbiguousScanner)
        )
        assert selected.address == second.address

    def test_live_path_collects_and_always_stops_streams(self, monkeypatch):
        monkeypatch.setattr(validation, "MIN_CAPTURE_SECONDS", 0.01)
        capture = asyncio.run(
            capture_live(
                duration_s=0.06,
                scanner=FakeScanner,
                client_factory=FakeClient,
                pmd_factory=FakePMD,
                hr_factory=FakeHeartRate,
            )
        )
        report = validate_capture(capture)

        assert capture.stream_start_succeeded is True
        assert capture.stream_stop_succeeded is True
        assert capture.hr_notify_start_succeeded is True
        assert capture.hr_notify_stop_succeeded is True
        assert FakePMD.instances[-1].stopped is True
        assert FakeHeartRate.instances[-1].stopped is True
        assert report["hardware_integration_status"] == "PASS_INTEGRATION_ONLY"
        assert report["clinical_accuracy_status"].startswith("NOT_EVALUATED")

    def test_start_failure_does_not_claim_hardware_pass(self, monkeypatch):
        monkeypatch.setattr(validation, "MIN_CAPTURE_SECONDS", 0.01)

        class StartFailurePMD(FakePMD):
            async def start_streaming(self, measurement):
                return 5, "INVALID PARAMETER", b""

        capture = asyncio.run(
            capture_live(
                duration_s=0.02,
                scanner=FakeScanner,
                client_factory=FakeClient,
                pmd_factory=StartFailurePMD,
                hr_factory=FakeHeartRate,
            )
        )
        report = validate_capture(capture)
        assert report["passed"] is False
        assert report["hardware_integration_status"] == "FAIL"
        assert FakeHeartRate.instances[-1].stopped is False
