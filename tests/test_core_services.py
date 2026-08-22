"""Behavior tests for the coordinator, SDR-control, and thermal services."""

import json
from types import SimpleNamespace

import pytest

import core.mqtt_coordinator as coordinator_mod
import core.sdr_control as sdr_mod
import core.thermal_monitor as thermal_mod


class FakeClient:
    def __init__(self):
        self.subscriptions = []
        self.published = []

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, *args, **kwargs):
        self.published.append((topic, payload))


def message(topic, payload):
    if not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    return SimpleNamespace(topic=topic, payload=payload)


@pytest.fixture
def coordinator(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "load_config", lambda: {
        "mqtt": {
            "broker": "broker.test", "port": 1884,
            "services": {"coordinator": {"username": "coord", "password": "secret"}},
        }
    })
    return coordinator_mod.MQTTCoordinator()


class TestCoordinator:
    def test_connect_subscribes_only_after_success(self, coordinator):
        client = FakeClient()
        coordinator._on_connect(client, None, None, 5)
        assert client.subscriptions == []
        coordinator._on_connect(client, None, None, 0)
        assert client.subscriptions == [coordinator_mod.TOPIC_WILDCARD]

    @pytest.mark.parametrize(
        ("topic", "payload", "state_key", "expected"),
        [
            ("shtf/df/bearing", {"bearing": 42}, "df_bearing", {"bearing": 42}),
            ("shtf/rx/recording", 1, "rx_recording", True),
            ("shtf/tx/status", "active", "tx_status", "active"),
        ],
    )
    def test_message_updates_state(self, coordinator, topic, payload, state_key, expected):
        coordinator._on_message(None, None, message(topic, payload))
        assert coordinator.state[state_key] == expected

    def test_pi_and_radar_topics_extract_identifiers(self, coordinator):
        coordinator._on_message(None, None, message("shtf/pi/pi-2/status", {"cpu": 51}))
        coordinator._on_message(None, None, message("shtf/radar/contacts/c-7", {"range": 20}))
        assert coordinator.state["pis"]["pi-2"]["data"] == {"cpu": 51}
        assert coordinator.state["radar_contacts"]["c-7"] == {"range": 20}

    def test_alarm_history_is_bounded(self, coordinator):
        for index in range(25):
            coordinator._on_message(None, None, message(coordinator_mod.TOPIC_ALARM, {"id": index}))
        assert len(coordinator.state["alarms"]) == 20
        assert coordinator.state["alarms"][0]["payload"] == {"id": 5}

    def test_non_json_payload_is_retained_as_text(self, coordinator):
        coordinator._on_message(None, None, message("shtf/tx/status", b"plain-status"))
        assert coordinator.state["tx_status"] == "plain-status"


@pytest.fixture
def sdr_service(monkeypatch):
    monkeypatch.setattr(sdr_mod, "load_config", lambda: {
        "mqtt": {"services": {"sdr_control": {"username": "sdr", "password": "secret"}}}
    })
    return sdr_mod.SDRControlService()


class TestSdrControl:
    def test_probe_combines_soapy_and_usb_results(self, monkeypatch):
        def fake_run(command, **kwargs):
            if command[0] == "SoapySDRUtil":
                return SimpleNamespace(stdout="HackRF RTLSDR")
            return SimpleNamespace(stdout="0bda:2838\n" * 5 + "0456:b673\n")

        monkeypatch.setattr(sdr_mod.subprocess, "run", fake_run)
        devices = sdr_mod.probe_sdr_devices()
        assert devices["hackrf"]["present"] is True
        assert devices["rtlsdr"]["present"] is True
        assert devices["kraken"] == {"present": True, "driver": "rtlsdr", "count": 5}
        assert devices["pluto"]["present"] is True

    def test_probe_failure_returns_safe_absent_shape(self, monkeypatch):
        monkeypatch.setattr(sdr_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        devices = sdr_mod.probe_sdr_devices()
        assert all(device["present"] is False for device in devices.values())

    def test_malformed_json_shapes_are_ignored(self, sdr_service):
        sdr_service._on_message(None, None, message(sdr_mod.TOPIC_SDR_CMD, b"not-json"))
        sdr_service._on_message(None, None, message(sdr_mod.TOPIC_SDR_CMD, ["probe"] ))

    def test_probe_command_publishes_status(self, sdr_service, monkeypatch):
        client = FakeClient()
        sdr_service._client = client
        monkeypatch.setattr(sdr_mod, "probe_sdr_devices", lambda: {"rtl": {"present": True}})
        sdr_service._on_message(None, None, message(sdr_mod.TOPIC_SDR_CMD, {"action": "probe"}))
        assert sdr_service._devices == {"rtl": {"present": True}}
        topic, payload = client.published[-1]
        assert topic == sdr_mod.TOPIC_SDR_STATUS
        assert json.loads(payload)["version"] == "1.2.0"

    def test_publish_without_client_is_noop(self, sdr_service):
        sdr_service._publish_status()


@pytest.fixture
def thermal_monitor(tmp_path, monkeypatch):
    config_path = tmp_path / "specter.json"
    config_path.write_text(json.dumps({
        "mqtt": {
            "broker": "broker.test", "port": 1884,
            "services": {"thermal": {"username": "thermal", "password": "secret"}},
        },
        "thermal": {"throttle_c": 70, "emergency_shutdown_c": 80},
    }))
    monkeypatch.setattr(thermal_mod, "CONFIG_PATH", config_path)
    return thermal_mod.ThermalMonitor()


class TestThermalMonitor:
    def test_configures_thresholds_and_dedicated_credentials(self, thermal_monitor):
        assert thermal_monitor._throttle_c == 70
        assert thermal_monitor._shutdown_c == 80
        assert (thermal_monitor.username, thermal_monitor.password) == ("thermal", "secret")

    def test_publish_serializes_objects_and_preserves_strings(self, thermal_monitor):
        client = FakeClient()
        thermal_monitor._client = client
        thermal_monitor._publish("object", {"temp": 71})
        thermal_monitor._publish("text", "already-json")
        assert json.loads(client.published[0][1]) == {"temp": 71}
        assert client.published[1][1] == "already-json"

    def test_alarm_is_rate_limited(self, thermal_monitor, monkeypatch):
        client = FakeClient()
        thermal_monitor._client = client
        monkeypatch.setattr(thermal_mod.time, "time", lambda: 1000)
        thermal_monitor._alarm("overtemp", "hot")
        thermal_monitor._alarm("overtemp", "still hot")
        assert len(client.published) == 1
        topic, payload = client.published[0]
        assert topic == thermal_mod.TOPIC_ALARM
        assert json.loads(payload)["msg"] == "hot"

    def test_non_object_config_is_rejected(self, tmp_path, monkeypatch):
        config_path = tmp_path / "specter.json"
        config_path.write_text("[]")
        monkeypatch.setattr(thermal_mod, "CONFIG_PATH", config_path)
        monkeypatch.setattr(thermal_mod, "load_config", lambda: {})
        with pytest.raises(RuntimeError, match="must be a JSON object"):
            thermal_mod.ThermalMonitor()
