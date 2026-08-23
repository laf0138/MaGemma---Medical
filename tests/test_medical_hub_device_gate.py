"""
Tests for the BLE device verification gate in medical/specter_medical_hub.py.

The device/characteristic UUIDs and byte-layout assumptions in
BluetoothDeviceConfig.DEVICES and MedicalDeviceParser were checked against
Bluetooth SIG specs and public reverse-engineering research and found to
very likely NOT match how these real devices communicate (see the
VERIFIED DEVICE GATE comment in the source - e.g. Omron's config points
at the generic Device Information Service with characteristic UUIDs that
are the real Bluetooth SIG assignments for Temperature/Humidity/Alert-
Category, and Masimo's parser claims to read the standard Heart Rate
Measurement characteristic but extracts an SpO2 field that characteristic
doesn't have). A plausible-looking wrong vital sign is worse than no
reading, so collect_from_device() now hard-blocks any device type not
explicitly marked verified - these tests confirm that block actually
prevents a BLE connection attempt, and that marking a device verified
lifts it. The gate applies uniformly to all five shipped device types,
including polar_h10 (see test_medical_hub_polar_h10.py) - using a real
library instead of hand-parsed bytes lowers the risk, it doesn't exempt
a device from needing a real-hardware confirmation before trusting it.
"""
import asyncio

import pytest

import medical.specter_medical_hub as hub_mod


class RecordingBleakClient:
    """Stands in for bleak.BleakClient. Records that a connection was
    attempted, then raises so the test doesn't need a real device/adapter."""
    instances = []

    def __init__(self, device):
        RecordingBleakClient.instances.append(device)

    async def __aenter__(self):
        raise RuntimeError("test stops here - connection attempt recorded")

    async def __aexit__(self, exc_type, exc, tb):
        return True  # swallow the RuntimeError, matching what collect_from_device does


@pytest.fixture(autouse=True)
def _reset_recording_client():
    RecordingBleakClient.instances.clear()
    yield
    RecordingBleakClient.instances.clear()


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(hub_mod, "_mqtt_credentials", lambda: ("test-user", "test-password"))
    h = hub_mod.MedicalHubBleCollector()
    h.discovered_devices = {
        "AA:BB:CC:DD:EE:FF": {
            "name": "Omron BP7450",
            "address": "AA:BB:CC:DD:EE:FF",
            "type": "omron_bp7450",
            "rssi": -50,
            "object": "fake-device-handle",
        }
    }
    monkeypatch.setattr(hub_mod, "BleakClient", RecordingBleakClient)
    return h


class TestUnverifiedDevicesAreBlocked:
    def test_default_verified_set_is_empty(self):
        # Guards against someone accidentally shipping a non-empty default -
        # every device type must be explicitly opted in.
        assert hub_mod.VERIFIED_DEVICE_TYPES == frozenset()

    def test_blocked_device_never_reaches_bleak_client(self, hub, monkeypatch):
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset())

        result = asyncio.run(hub.collect_from_device("AA:BB:CC:DD:EE:FF"))

        assert result is None
        assert RecordingBleakClient.instances == []

    def test_all_shipped_device_types_are_blocked_by_default(self, hub, monkeypatch):
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset())
        for dev_type in hub_mod.BluetoothDeviceConfig.DEVICES:
            hub.discovered_devices["addr"] = {
                "name": dev_type, "address": "addr", "type": dev_type,
                "rssi": -50, "object": "fake-device-handle",
            }
            result = asyncio.run(hub.collect_from_device("addr"))
            assert result is None, f"{dev_type} should be blocked by default"
        assert RecordingBleakClient.instances == []


class TestVerifiedDeviceIsAllowedThrough:
    def test_marking_device_verified_allows_connection_attempt(self, hub, monkeypatch):
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"omron_bp7450"}))

        asyncio.run(hub.collect_from_device("AA:BB:CC:DD:EE:FF"))

        # Reaching RecordingBleakClient proves the gate let this device
        # type through - the connection then fails deliberately (no real
        # adapter/hardware here), which collect_from_device already
        # handles via its own try/except.
        assert RecordingBleakClient.instances == ["fake-device-handle"]

    def test_verifying_one_device_does_not_unblock_others(self, hub, monkeypatch):
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"contour_next_one"}))

        result = asyncio.run(hub.collect_from_device("AA:BB:CC:DD:EE:FF"))  # type: omron_bp7450

        assert result is None
        assert RecordingBleakClient.instances == []


class TestVerifiedDeviceTypesEnvVarParsing:
    def test_comma_separated_list_parses_to_frozenset(self, monkeypatch):
        monkeypatch.setenv("SPECTER_VERIFIED_BLE_DEVICES", "omron_bp7450, contour_next_one ,")
        import importlib
        reloaded = importlib.reload(hub_mod)
        try:
            assert reloaded.VERIFIED_DEVICE_TYPES == frozenset({"omron_bp7450", "contour_next_one"})
        finally:
            monkeypatch.delenv("SPECTER_VERIFIED_BLE_DEVICES", raising=False)
            importlib.reload(hub_mod)  # restore the empty-default module state

    def test_unset_env_var_yields_empty_set(self, monkeypatch):
        monkeypatch.delenv("SPECTER_VERIFIED_BLE_DEVICES", raising=False)
        import importlib
        reloaded = importlib.reload(hub_mod)
        assert reloaded.VERIFIED_DEVICE_TYPES == frozenset()
