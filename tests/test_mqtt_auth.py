"""
Regression tests for MQTT broker authentication wiring.

The broker used to run with allow_anonymous=true and no password (see
docs/MANUAL.md Part 7.2) - anyone on the LAN could read every patient's
vitals/diagnosis or publish a forged trauma command. Every client module
now calls username_pw_set() before connecting, resolving credentials from
/etc/specter/specter.json (written by the installer) with a documented
fallback default. These tests lock in that wiring so a future refactor
can't silently drop the auth call.

Where a module's client connects immediately on construction (LibraryMQTT,
MQTTClient), paho's Client.connect/loop_start are patched to no-ops so the
test never attempts a real network connection - the assertion is on
username_pw_set having been applied to the client object, not on the
(irrelevant, always-mocked-out) connection outcome.
"""
import logging

import pytest

import core.mqtt_coordinator as coordinator_mod
import core.sdr_control as sdr_mod
import core.thermal_monitor as thermal_mod
import dashboard.dashboard_server as dashboard_mod
import medical.specter_medical_ai as medical_ai_mod
import medical.specter_medical_hub as medical_hub_mod
import services.library_api as library_mod
import services.specter_rx_ring_buffer as rx_mod
import trauma.specter_trauma as trauma_mod
import trauma.specter_trauma_monitor as trauma_monitor_mod


class TestTraumaServiceAuth:
    def test_applies_credentials_from_helper(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trauma_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        svc = trauma_mod.TraumaService("broker-host", 1883, str(tmp_path / "scene.json"))
        assert svc.mqtt.username == "tuser"
        assert svc.mqtt.password == "tpass"


class TestMedicalAIEngineAuth:
    def test_applies_credentials_from_helper(self, monkeypatch):
        monkeypatch.setattr(medical_ai_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        cfg = medical_ai_mod.Config(chroma_path="/does/not/exist")
        engine = medical_ai_mod.MedicalAIEngine(cfg)
        assert engine.mqtt.username == "tuser"
        assert engine.mqtt.password == "tpass"


class TestMedicalHubAuth:
    def test_applies_credentials_from_helper(self, monkeypatch):
        monkeypatch.setattr(medical_hub_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        hub = medical_hub_mod.MedicalHubBleCollector()
        assert hub.mqtt_client.username == "tuser"
        assert hub.mqtt_client.password == "tpass"


class TestTraumaMonitorCredentialsHelper:
    """main() connects and loops forever, so it isn't safely callable in a
    test - the credential-resolution helper it calls is tested directly."""

    def test_falls_back_to_documented_default_when_config_unreadable(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(pathlib.Path, "read_text",
                             lambda self: (_ for _ in ()).throw(FileNotFoundError()))
        assert trauma_monitor_mod._mqtt_credentials() == (
            trauma_monitor_mod.MQTT_DEFAULT_USERNAME,
            trauma_monitor_mod.MQTT_DEFAULT_PASSWORD,
        )

    def test_uses_config_file_credentials_when_present(self, tmp_path, monkeypatch):
        import pathlib
        content = '{"mqtt": {"username": "cuser", "password": "cpass"}}'
        monkeypatch.setattr(pathlib.Path, "read_text", lambda self: content)
        assert trauma_monitor_mod._mqtt_credentials() == ("cuser", "cpass")


class TestLibraryMQTTAuth:
    def test_applies_credentials_on_connect(self, monkeypatch):
        monkeypatch.setattr(library_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        monkeypatch.setattr("paho.mqtt.client.Client.connect", lambda self, *a, **k: None)
        monkeypatch.setattr("paho.mqtt.client.Client.loop_start", lambda self: None)

        bridge = library_mod.LibraryMQTT("broker-host")

        assert bridge._client is not None
        assert bridge._client.username == "tuser"
        assert bridge._client.password == "tpass"


class TestRxRingBufferMQTTClientAuth:
    def test_applies_credentials_on_connect(self, monkeypatch):
        monkeypatch.setattr(rx_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        monkeypatch.setattr("paho.mqtt.client.Client.connect", lambda self, *a, **k: None)
        monkeypatch.setattr("paho.mqtt.client.Client.loop_start", lambda self: None)

        client = rx_mod.MQTTClient(
            "broker-host", 1883, trigger_cb=lambda reason="manual": None,
            log=logging.getLogger("test-rx"),
        )

        assert client._client is not None
        assert client._client.username == "tuser"
        assert client._client.password == "tpass"


class TestMqttCoordinatorAuthResolution:
    """run() starts blocking loops, so credential resolution in __init__ is
    tested directly rather than the full connect path."""

    def test_reads_credentials_from_config(self, monkeypatch):
        monkeypatch.setattr(coordinator_mod, "load_config", lambda: {
            "mqtt": {"broker": "10.0.0.1", "port": 1883, "username": "cuser", "password": "cpass"}
        })
        c = coordinator_mod.MQTTCoordinator()
        assert c.username == "cuser"
        assert c.password == "cpass"

    def test_falls_back_to_documented_default(self, monkeypatch):
        monkeypatch.setattr(coordinator_mod, "load_config", lambda: {"mqtt": {}})
        c = coordinator_mod.MQTTCoordinator()
        assert c.username == coordinator_mod.MQTT_DEFAULT_USERNAME
        assert c.password == coordinator_mod.MQTT_DEFAULT_PASSWORD


class TestSdrControlAuthResolution:
    def test_reads_credentials_from_config(self, monkeypatch):
        monkeypatch.setattr(sdr_mod, "load_config", lambda: {
            "mqtt": {"username": "cuser", "password": "cpass"}
        })
        svc = sdr_mod.SDRControlService()
        assert svc.username == "cuser"
        assert svc.password == "cpass"

    def test_falls_back_to_documented_default(self, monkeypatch):
        monkeypatch.setattr(sdr_mod, "load_config", lambda: {})
        svc = sdr_mod.SDRControlService()
        assert svc.username == sdr_mod.MQTT_DEFAULT_USERNAME
        assert svc.password == sdr_mod.MQTT_DEFAULT_PASSWORD


class TestThermalMonitorAuthResolution:
    def test_reads_credentials_from_config_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "specter.json"
        cfg_file.write_text('{"mqtt": {"username": "cuser", "password": "cpass"}}')
        monkeypatch.setattr(thermal_mod, "CONFIG_PATH", cfg_file)
        monkeypatch.setattr(thermal_mod, "load_config", lambda: {})
        mon = thermal_mod.ThermalMonitor()
        assert mon.username == "cuser"
        assert mon.password == "cpass"

    def test_falls_back_to_documented_default_when_config_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(thermal_mod, "CONFIG_PATH", tmp_path / "does_not_exist.json")
        monkeypatch.setattr(thermal_mod, "load_config", lambda: {})
        mon = thermal_mod.ThermalMonitor()
        assert mon.username == thermal_mod.MQTT_DEFAULT_USERNAME
        assert mon.password == thermal_mod.MQTT_DEFAULT_PASSWORD


class TestDashboardMQTTAuth:
    def test_defaults_when_not_specified(self):
        m = dashboard_mod.DashboardMQTT(broker="192.168.1.1", port=1883)
        assert m.username == dashboard_mod.MQTT_DEFAULT_USERNAME
        assert m.password == dashboard_mod.MQTT_DEFAULT_PASSWORD

    def test_explicit_credentials_are_stored(self):
        m = dashboard_mod.DashboardMQTT(
            broker="192.168.1.1", port=1883, username="cuser", password="cpass")
        assert m.username == "cuser"
        assert m.password == "cpass"
