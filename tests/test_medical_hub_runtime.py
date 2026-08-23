"""Hardware-free tests for medical-hub I/O, lifecycle, and failure paths."""

import argparse
import asyncio
import json
from types import SimpleNamespace

import pytest

import medical.specter_medical_hub as hub_mod
from medical.specter_medical_hub import PatientVitals, VitalSign


class FakeMqtt:
    def __init__(self):
        self.calls = []
        self.subscriptions = []
        self.fail_publish = False

    def connect(self, host, port, keepalive):
        self.calls.append(("connect", host, port, keepalive))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, qos=0):
        if self.fail_publish:
            raise RuntimeError("broker unavailable")
        self.calls.append(("publish", topic, payload, qos))


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(
        hub_mod, "_mqtt_credentials", lambda: ("test-user", "test-password")
    )
    value = hub_mod.MedicalHubBleCollector()
    value.mqtt_client = FakeMqtt()
    return value


class TestPayloadsAndCredentials:
    def test_mqtt_1x_constructor_fallback(self, monkeypatch):
        sentinel = object()

        def client_constructor(*args, **kwargs):
            if args:
                raise AttributeError("CallbackAPIVersion unavailable")
            assert kwargs["client_id"] == "hub"
            return sentinel

        monkeypatch.setattr(hub_mod.mqtt, "Client", client_constructor)
        assert hub_mod._mqtt_client("hub") is sentinel

    def test_vital_and_snapshot_json_round_trip(self):
        vital = VitalSign("device", "pulse", 72, "bpm", "2026-01-01T00:00:00Z", -42)
        assert json.loads(vital.to_mqtt_payload())["value"] == 72
        snapshot = PatientVitals("p1", "2026-01-01T00:00:00Z", [vital])
        body = json.loads(snapshot.to_mqtt_payload())
        assert body["patient_id"] == "p1"
        assert body["readings"][0]["unit"] == "bpm"

    def test_credentials_accept_dedicated_non_placeholder_values(self, monkeypatch):
        monkeypatch.setattr(
            hub_mod.Path,
            "read_text",
            lambda self: json.dumps(
                {"mqtt": {"services": {"medical_hub": {
                    "username": "hub-user", "password": "secret"
                }}}}
            ),
        )
        assert hub_mod._mqtt_credentials() == ("hub-user", "secret")

    @pytest.mark.parametrize(
        "contents",
        [
            "not-json",
            json.dumps({"mqtt": {"services": {"medical_hub": {}}}}),
            json.dumps({"mqtt": {"services": {"medical_hub": {
                "username": "hub", "password": hub_mod.MQTT_DEFAULT_PASSWORD
            }}}}),
        ],
    )
    def test_credentials_fail_closed(self, monkeypatch, contents):
        monkeypatch.setattr(hub_mod.Path, "read_text", lambda self: contents)
        with pytest.raises(RuntimeError):
            hub_mod._mqtt_credentials()


class TestBluetoothScanning:
    def test_documented_bleak_return_adv_shape_is_decoded(self, hub, monkeypatch):
        device = SimpleNamespace(name="Polar H10 ABC123", address="AA:BB")
        advertisement = SimpleNamespace(local_name="Polar H10 ABC123", rssi=-47)

        class Scanner:
            async def discover(self, timeout, return_adv):
                assert timeout == 3
                assert return_adv is True
                return {device.address: (device, advertisement)}

        monkeypatch.setattr(hub_mod, "BleakScanner", Scanner)
        found = asyncio.run(hub.scan_devices(timeout=3))
        assert found["AA:BB"]["type"] == "polar_h10"
        assert found["AA:BB"]["rssi"] == -47
        assert found["AA:BB"]["object"] is device

    def test_advertised_local_name_fallback_and_unmatched_device(self, hub, monkeypatch):
        matched = SimpleNamespace(name=None, address="AA:01")
        other = SimpleNamespace(name="Keyboard", address="AA:02")

        class Scanner:
            async def discover(self, **kwargs):
                return {
                    "AA:01": (matched, SimpleNamespace(local_name="Contour Meter", rssi=-55)),
                    "AA:02": (other, SimpleNamespace(local_name="Keyboard", rssi=-20)),
                }

        monkeypatch.setattr(hub_mod, "BleakScanner", Scanner)
        found = asyncio.run(hub.scan_devices())
        assert list(found) == ["AA:01"]
        assert found["AA:01"]["type"] == "contour_next_one"

    def test_legacy_list_shape_is_supported(self, hub, monkeypatch):
        device = SimpleNamespace(name="Polar H10 OLD", address="AA:03", rssi=-60)

        class Scanner:
            async def discover(self, **kwargs):
                return [device]

        monkeypatch.setattr(hub_mod, "BleakScanner", Scanner)
        assert asyncio.run(hub.scan_devices())["AA:03"]["rssi"] == -60

    def test_scanner_failure_returns_empty_result(self, hub, monkeypatch):
        class Scanner:
            async def discover(self, **kwargs):
                raise RuntimeError("Bluetooth adapter unavailable")

        monkeypatch.setattr(hub_mod, "BleakScanner", Scanner)
        assert asyncio.run(hub.scan_devices()) == {}


class WorkingClient:
    payloads = {}
    read_errors = set()

    def __init__(self, device):
        self.device = device

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def read_gatt_char(self, uuid):
        if uuid in self.read_errors:
            raise RuntimeError("read failed")
        return self.payloads[uuid]


class TestDeviceCollection:
    @pytest.fixture(autouse=True)
    def _reset_client(self):
        WorkingClient.payloads = {}
        WorkingClient.read_errors = set()

    def _discover_contour(self, hub):
        hub.discovered_devices = {
            "meter": {
                "name": "Contour Next One",
                "address": "meter",
                "type": "contour_next_one",
                "rssi": -51,
                "object": object(),
            }
        }

    def test_unknown_address_is_ignored(self, hub):
        assert asyncio.run(hub.collect_from_device("missing")) is None

    def test_verified_characteristic_read_and_parser_result(self, hub, monkeypatch):
        self._discover_contour(hub)
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"contour_next_one"}))
        monkeypatch.setattr(hub_mod, "BleakClient", WorkingClient)
        WorkingClient.payloads["2a18"] = (
            bytes([0x02, 1, 0])
            + bytes([0xEA, 0x07, 8, 21, 12, 0, 0])
            + bytes([0xBF, 0xA3, 0])
        )
        result = asyncio.run(hub.collect_from_device("meter"))
        assert result["readings"] == {"glucose_mg_dl": 95.9}
        assert result["rssi"] == -51

    def test_characteristic_read_failure_produces_no_reading(self, hub, monkeypatch):
        self._discover_contour(hub)
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"contour_next_one"}))
        monkeypatch.setattr(hub_mod, "BleakClient", WorkingClient)
        WorkingClient.read_errors.add("2a18")
        assert asyncio.run(hub.collect_from_device("meter")) is None

    def test_parser_failure_is_contained(self, hub, monkeypatch):
        self._discover_contour(hub)
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"contour_next_one"}))
        monkeypatch.setattr(hub_mod, "BleakClient", WorkingClient)
        WorkingClient.payloads["2a18"] = b"payload"
        monkeypatch.setattr(
            hub.parser,
            "parse_contour_glucometer",
            lambda payload: (_ for _ in ()).throw(ValueError("bad parser")),
        )
        assert asyncio.run(hub.collect_from_device("meter")) is None

    def test_connection_loss_is_contained(self, hub, monkeypatch):
        self._discover_contour(hub)
        monkeypatch.setattr(hub_mod, "VERIFIED_DEVICE_TYPES", frozenset({"contour_next_one"}))

        class LostClient(WorkingClient):
            async def __aenter__(self):
                raise ConnectionError("device disappeared")

        monkeypatch.setattr(hub_mod, "BleakClient", LostClient)
        assert asyncio.run(hub.collect_from_device("meter")) is None


class TestAggregationAndPublishing:
    def test_multiple_devices_and_unit_mapping(self, hub):
        hub.patient_id = "patient-a"
        hub.discovered_devices = {"one": {}, "two": {}}

        async def collect(address):
            if address == "one":
                return {
                    "device_name": "H10", "rssi": -40,
                    "readings": {
                        "pulse": 72,
                        "ecg_q_s_peak_interval_ms": 84.6,
                        "ecg_analysis_status": "experimental_not_clinically_validated",
                    },
                }
            return {
                "device_name": "Meter", "rssi": -50,
                "readings": {"glucose_mg_dl": 95.9, "custom": 1},
            }

        hub.collect_from_device = collect
        vitals = asyncio.run(hub.collect_all_devices())
        by_type = {reading.reading_type: reading for reading in vitals.readings}
        assert vitals.patient_id == "patient-a"
        assert by_type["pulse"].unit == "bpm"
        assert by_type["ecg_q_s_peak_interval_ms"].unit == "ms"
        assert by_type["ecg_analysis_status"].unit == "status"
        assert by_type["glucose_mg_dl"].unit == "mg/dL"
        assert by_type["custom"].unit == "unknown"
        assert len({reading.timestamp_utc for reading in vitals.readings}) == 1

    def test_disconnected_publish_is_suppressed(self, hub):
        hub.mqtt_connected = False
        hub.publish_vitals(PatientVitals("p", "now", []))
        assert not any(call[0] == "publish" for call in hub.mqtt_client.calls)

    def test_snapshot_and_individual_topics_are_published(self, hub):
        hub.patient_id = "p1"
        hub.mqtt_connected = True
        reading = VitalSign("meter", "pulse", 72, "bpm", "now", -40)
        hub.publish_vitals(PatientVitals("p1", "now", [reading]))
        publishes = [call for call in hub.mqtt_client.calls if call[0] == "publish"]
        assert [call[1] for call in publishes] == [
            "shtf/medical/vitals/p1",
            "shtf/medical/vitals/p1/pulse",
        ]
        assert all(call[3] == 1 for call in publishes)

    def test_publish_failure_is_contained(self, hub):
        hub.mqtt_connected = True
        hub.mqtt_client.fail_publish = True
        hub.publish_vitals(PatientVitals("p", "now", []))


class TestMqttLifecycle:
    def test_connect_success_subscribes_and_updates_state(self, hub):
        hub._on_mqtt_connect(hub.mqtt_client, None, None, 0)
        assert hub.mqtt_connected is True
        assert hub.mqtt_client.subscriptions == ["shtf/medical/hub/command/#"]

    def test_connect_failure_and_disconnect_forms_clear_state(self, hub):
        hub._on_mqtt_connect(hub.mqtt_client, None, None, 5)
        assert hub.mqtt_connected is False
        hub.mqtt_connected = True
        hub._on_mqtt_disconnect(hub.mqtt_client, None, 7)
        assert hub.mqtt_connected is False
        hub.mqtt_connected = True
        hub._on_mqtt_disconnect(hub.mqtt_client, None, object(), 8)
        assert hub.mqtt_connected is False

    def test_patient_command_and_malformed_message(self, hub):
        message = SimpleNamespace(
            topic="shtf/medical/hub/command/patient_id",
            payload=json.dumps({"patient_id": "patient-b"}).encode(),
        )
        hub._on_mqtt_message(None, None, message)
        assert hub.patient_id == "patient-b"
        message.payload = b"not-json"
        hub._on_mqtt_message(None, None, message)
        assert hub.patient_id == "patient-b"

    def test_connect_disconnect_methods_drive_client(self, hub, monkeypatch):
        monkeypatch.setattr(hub_mod.time, "sleep", lambda seconds: None)
        hub.connect_mqtt()
        hub.disconnect_mqtt()
        assert ("connect", "192.168.1.1", 1883, 60) in hub.mqtt_client.calls
        assert ("loop_start",) in hub.mqtt_client.calls
        assert ("loop_stop",) in hub.mqtt_client.calls
        assert ("disconnect",) in hub.mqtt_client.calls

    def test_connect_failure_is_contained(self, hub, monkeypatch):
        hub.mqtt_client.connect = lambda *args, **kwargs: (_ for _ in ()).throw(
            ConnectionError("broker down")
        )
        monkeypatch.setattr(hub_mod.time, "sleep", lambda seconds: None)
        hub.connect_mqtt()


class TestMainLoopShutdown:
    def test_empty_scan_shutdown_disconnects(self, monkeypatch):
        instances = []

        class LoopHub:
            patient_id = "default"

            def __init__(self, **kwargs):
                self.disconnected = False
                instances.append(self)

            def connect_mqtt(self):
                pass

            async def scan_devices(self, timeout):
                return {}

            def disconnect_mqtt(self):
                self.disconnected = True

        async def interrupt(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(hub_mod, "MedicalHubBleCollector", LoopHub)
        monkeypatch.setattr(hub_mod.asyncio, "sleep", interrupt)
        args = argparse.Namespace(
            mqtt_host="host", mqtt_port=1883, scan_timeout=1, cycle_interval=1
        )
        asyncio.run(hub_mod.main_async(args))
        assert instances[0].disconnected is True

    def test_collection_cycle_publishes_then_shuts_down(self, monkeypatch):
        instances = []
        reading = VitalSign("device", "pulse", 72, "bpm", "now")

        class LoopHub:
            patient_id = "p1"

            def __init__(self, **kwargs):
                self.published = []
                self.disconnected = False
                instances.append(self)

            def connect_mqtt(self):
                pass

            async def scan_devices(self, timeout):
                return {"device": {}}

            async def collect_all_devices(self):
                return PatientVitals("p1", "now", [reading])

            def publish_vitals(self, vitals):
                self.published.append(vitals)

            def disconnect_mqtt(self):
                self.disconnected = True

        async def interrupt(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(hub_mod, "MedicalHubBleCollector", LoopHub)
        monkeypatch.setattr(hub_mod.asyncio, "sleep", interrupt)
        args = argparse.Namespace(
            mqtt_host="host", mqtt_port=1883, scan_timeout=1, cycle_interval=1
        )
        asyncio.run(hub_mod.main_async(args))
        assert instances[0].published
        assert instances[0].disconnected is True

    def test_empty_collection_and_unexpected_error_both_disconnect(self, monkeypatch):
        instances = []

        class LoopHub:
            patient_id = "p1"

            def __init__(self, **kwargs):
                self.collections = 0
                self.disconnected = False
                instances.append(self)

            def connect_mqtt(self):
                pass

            async def scan_devices(self, timeout):
                return {"device": {}}

            async def collect_all_devices(self):
                self.collections += 1
                if self.collections == 1:
                    return PatientVitals("p1", "now", [])
                raise ConnectionError("Bluetooth controller lost")

            def publish_vitals(self, vitals):
                raise AssertionError("empty vitals must not publish")

            def disconnect_mqtt(self):
                self.disconnected = True

        async def no_wait(_seconds):
            return None

        monkeypatch.setattr(hub_mod, "MedicalHubBleCollector", LoopHub)
        monkeypatch.setattr(hub_mod.asyncio, "sleep", no_wait)
        args = argparse.Namespace(
            mqtt_host="host", mqtt_port=1883, scan_timeout=1, cycle_interval=1
        )
        asyncio.run(hub_mod.main_async(args))
        assert instances[0].collections == 2
        assert instances[0].disconnected is True
