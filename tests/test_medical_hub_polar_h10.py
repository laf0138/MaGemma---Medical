"""
Tests for the Polar H10 ECG/heart-rate streaming path in
medical/specter_medical_hub.py, added to replace the hand-parsed byte
offsets used for the other four devices with bleakheart - a maintained
library for Polar's PMD interface (confirmed against its actual installed
source, not just its README, since that's exactly the class of mistake
that produced the other four devices' fabricated parsers).

PolarMeasurementData/HeartRate are stood in for with fakes that mirror
their real public interface (constructor kwargs, start_streaming/
stop_streaming/start_notify/stop_notify) rather than mocked wholesale -
this tests that SPECTER's own consumption code matches bleakheart's
documented queue-item shapes:
  ECG:          ('ECG', tstamp_ns, [int_microvolt_samples...])
  HR (unpack=False): ('HR', tstamp_ns, (avg_hr_bpm, [rr_ms...]), energy_kj)
"""
import asyncio

import pytest

import medical.specter_medical_hub as hub_mod


class FakePMD:
    """Mirrors bleakheart.PolarMeasurementData's public interface."""
    instances = []

    def __init__(self, client, ecg_queue=None, acc_queue=None,
                 ppg_queue=None, raw_queue=None, callback=None):
        self.client = client
        self.ecg_queue = ecg_queue
        self.start_streaming_calls = []
        self.stop_streaming_calls = []
        self.start_result = (0, "", b"")
        FakePMD.instances.append(self)

    async def start_streaming(self, measurement, **settings):
        self.start_streaming_calls.append(measurement)
        return self.start_result

    async def stop_streaming(self, measurement):
        self.stop_streaming_calls.append(measurement)
        return (0, "")


class FakeHeartRate:
    """Mirrors bleakheart.HeartRate's public interface."""
    instances = []

    def __init__(self, client, queue=None, callback=None,
                 contact_callback=None, contact_lost_callback=None,
                 instant_rate=False, unpack=True):
        self.client = client
        self.queue = queue
        self.unpack = unpack
        self.notify_started = False
        self.notify_stopped = False
        FakeHeartRate.instances.append(self)

    async def start_notify(self, filter_nocontact=False):
        self.notify_started = True

    async def stop_notify(self):
        self.notify_stopped = True


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch):
    FakePMD.instances.clear()
    FakeHeartRate.instances.clear()
    monkeypatch.setattr(hub_mod, "PolarMeasurementData", FakePMD)
    monkeypatch.setattr(hub_mod, "HeartRate", FakeHeartRate)
    yield


@pytest.fixture
def hub():
    h = hub_mod.MedicalHubBleCollector()
    h.polar_stream_seconds = 0.05  # keep tests fast
    return h


class TestEcgWaveformCollection:
    def test_ecg_samples_accumulate_across_frames(self, hub):
        async def run():
            device_info = {"name": "Polar H10 ABC", "type": "polar_h10"}
            # Pre-populate the queue FakePMD will be given, matching
            # bleakheart's real documented ('ECG', tstamp_ns, payload) shape.
            result_holder = {}

            orig_stream = hub._collect_polar_h10_stream

            async def collect_and_feed():
                task = asyncio.ensure_future(hub._collect_polar_h10_stream(object(), device_info))
                await asyncio.sleep(0)  # let it construct FakePMD/FakeHeartRate and start streaming
                ecg_queue = FakePMD.instances[-1].ecg_queue
                await ecg_queue.put(('ECG', 1_000_000, [1, 2, 3]))
                await ecg_queue.put(('ECG', 1_100_000, [4, 5, 6]))
                return await task

            result_holder["result"] = await collect_and_feed()
            return result_holder["result"]

        result = asyncio.run(run())
        assert result["ecg_waveform_uv"] == [1, 2, 3, 4, 5, 6]

    def test_empty_result_when_nothing_arrives(self, hub):
        result = asyncio.run(hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"}))
        assert result == {}

    def test_start_streaming_error_returns_empty_and_skips_hr(self, hub):
        async def run():
            # Configure the error before construction happens inside the
            # method under test, by patching the class's default.
            original_init = FakePMD.__init__

            def broken_init(self, *a, **kw):
                original_init(self, *a, **kw)
                self.start_result = (1, "device busy", b"")
            FakePMD.__init__ = broken_init
            try:
                return await hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"})
            finally:
                FakePMD.__init__ = original_init

        result = asyncio.run(run())
        assert result == {}
        assert FakeHeartRate.instances[-1].notify_started is False

    def test_ecg_stream_started_and_stopped(self, hub):
        asyncio.run(hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"}))
        assert FakePMD.instances[-1].start_streaming_calls == ['ECG']
        assert FakePMD.instances[-1].stop_streaming_calls == ['ECG']

    def test_hr_notify_started_and_stopped(self, hub):
        asyncio.run(hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"}))
        assert FakeHeartRate.instances[-1].notify_started is True
        assert FakeHeartRate.instances[-1].notify_stopped is True


class TestHeartRateCollection:
    def test_heart_rate_constructed_with_unpack_false(self, hub):
        asyncio.run(hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"}))
        # unpack=False is a deliberate choice - see the comment in
        # _collect_polar_h10_stream - because that shape is unambiguous
        # (full RR list per frame) vs. unpack=True's per-heartbeat splitting.
        assert FakeHeartRate.instances[-1].unpack is False

    def test_hr_frame_captures_average_and_rr_intervals(self, hub):
        async def run():
            task = asyncio.ensure_future(
                hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"})
            )
            await asyncio.sleep(0)
            hr_queue = FakeHeartRate.instances[-1].queue
            # Real documented unpack=False shape: ('HR', tstamp, (avghr, rrlist), energy)
            await hr_queue.put(('HR', 1_000_000, (72, [810, 795]), 5.2))
            return await task

        result = asyncio.run(run())
        assert result["pulse"] == 72
        assert result["rr_intervals_ms"] == [810, 795]

    def test_multiple_hr_frames_use_latest_average_but_accumulate_rr(self, hub):
        async def run():
            task = asyncio.ensure_future(
                hub._collect_polar_h10_stream(object(), {"name": "Polar H10", "type": "polar_h10"})
            )
            await asyncio.sleep(0)
            hr_queue = FakeHeartRate.instances[-1].queue
            await hr_queue.put(('HR', 1_000_000, (70, [820]), 5.0))
            await hr_queue.put(('HR', 1_100_000, (74, [790]), 5.1))
            return await task

        result = asyncio.run(run())
        assert result["pulse"] == 74
        assert result["rr_intervals_ms"] == [820, 790]


class TestCollectFromDeviceDispatch:
    def test_streaming_device_type_is_gated_like_any_other(self, monkeypatch, hub):
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset())
        hub.discovered_devices["addr"] = {
            "name": "Polar H10", "address": "addr", "type": "polar_h10",
            "rssi": -50, "object": "fake-device-handle",
        }
        result = asyncio.run(hub.collect_from_device("addr"))
        assert result is None
        assert FakePMD.instances == []

    def test_verified_streaming_device_dispatches_to_polar_collection(self, monkeypatch, hub):
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"polar_h10"}))
        monkeypatch.setattr(hub_mod, "BleakClient", _FakeBleakClientCtx)
        hub.discovered_devices["addr"] = {
            "name": "Polar H10", "address": "addr", "type": "polar_h10",
            "rssi": -50, "object": "fake-device-handle",
        }

        async def run():
            task = asyncio.ensure_future(hub.collect_from_device("addr"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if FakePMD.instances:
                await FakePMD.instances[-1].ecg_queue.put(('ECG', 1_000_000, [10, 20]))
            return await task

        result = asyncio.run(run())
        assert result is not None
        assert result["device_type"] == "polar_h10"
        assert result["readings"]["ecg_waveform_uv"] == [10, 20]


class _FakeBleakClientCtx:
    """Stands in for `async with BleakClient(device) as client:`."""
    def __init__(self, device):
        self.device = device

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False
