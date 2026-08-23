"""
Regression tests for MQTT broker authentication and per-service ACL
credential wiring.

The broker used to run with allow_anonymous=true and no password (see
docs/MANUAL.md Part 7.2) - anyone on the LAN could read every patient's
vitals/diagnosis or publish a forged trauma command. Auth alone isn't
enough on its own once every service shares one credential, though: a
leaked dashboard password (broad READ, many exposure points) would still
let someone masquerade as the trauma service. So each service resolves
its OWN username/password from specter.json's mqtt.services.<key> entry
(written by the installer with a dedicated least-privilege Mosquitto ACL
account). Missing or placeholder credentials fail closed, and every client
calls username_pw_set() with the result before connecting.

Where a module's client connects immediately on construction (LibraryMQTT,
MQTTClient), paho's Client.connect/loop_start are patched to no-ops so the
test never attempts a real network connection - the assertion is on
username_pw_set having been applied to the client object, not on the
(irrelevant, always-mocked-out) connection outcome.
"""
import json
import logging

import paho.mqtt.client as paho_mqtt
import pytest

import core.mqtt_coordinator as coordinator_mod
import core.sdr_control as sdr_mod
import core.thermal_monitor as thermal_mod
import dashboard.dashboard_server as dashboard_mod
import medical.specter_medical_ai as medical_ai_mod
import medical.specter_medical_hub as medical_hub_mod
import medical.specter_ecg_ai as ecg_ai_mod
import mesh.specter_mesh_relay as mesh_mod
import services.library_api as library_mod
import services.specter_rx_ring_buffer as rx_mod
import trauma.specter_trauma as trauma_mod
import trauma.specter_trauma_monitor as trauma_monitor_mod
import ward.specter_ward as ward_mod


# ---------------------------------------------------------------------------
# Each service's own default account name - guards against a copy/paste
# regression collapsing everyone back onto the same generic "specter" user.
# ---------------------------------------------------------------------------

class TestPerServiceDefaultUsernamesAreDistinct:
    def test_defaults_are_dedicated_not_shared(self):
        defaults = {
            "trauma": trauma_mod.MQTT_DEFAULT_USERNAME,
            "ward": ward_mod.MQTT_DEFAULT_USERNAME,
            "mesh": mesh_mod.MQTT_DEFAULT_USERNAME,
            "medical_ai": medical_ai_mod.MQTT_DEFAULT_USERNAME,
            "medical_hub": medical_hub_mod.MQTT_DEFAULT_USERNAME,
            "ecg_ai": ecg_ai_mod.MQTT_DEFAULT_USERNAME,
            "coordinator": coordinator_mod.MQTT_DEFAULT_USERNAME,
            "sdr_control": sdr_mod.MQTT_DEFAULT_USERNAME,
            "thermal": thermal_mod.MQTT_DEFAULT_USERNAME,
            "dashboard": dashboard_mod.MQTT_DEFAULT_USERNAME,
            "library_api": library_mod.MQTT_DEFAULT_USERNAME,
            "rx_buffer": rx_mod.MQTT_DEFAULT_USERNAME,
        }
        assert len(set(defaults.values())) == len(defaults), defaults
        for key, username in defaults.items():
            assert username == f"specter-{key.replace('_', '-')}"


# ---------------------------------------------------------------------------
# _mqtt_credentials() resolution priority (free-function modules)
# ---------------------------------------------------------------------------

def write_specter_json(monkeypatch, content: dict):
    """Make Path("/etc/specter/specter.json").read_text() return the given
    content, without recursing (the lambda must not itself call .read_text()
    on any Path, since that would re-enter the same patched method)."""
    import pathlib
    text = json.dumps(content)
    monkeypatch.setattr(pathlib.Path, "read_text", lambda self: text)


class TestTraumaCredentialsResolution:
    def test_service_entry_wins_over_flat_and_default(self, monkeypatch):
        write_specter_json(monkeypatch, {
            "mqtt": {
                "username": "specter-operator", "password": "operator-pass",
                "services": {"trauma": {"username": "specter-trauma", "password": "svc-pass"}},
            }
        })
        assert trauma_mod._mqtt_credentials() == ("specter-trauma", "svc-pass")

    def test_rejects_flat_operator_fields_when_service_entry_missing(self, monkeypatch):
        write_specter_json(monkeypatch, {
            "mqtt": {"username": "specter-operator", "password": "operator-pass"}
        })
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            trauma_mod._mqtt_credentials()

    def test_rejects_missing_config(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(pathlib.Path, "read_text",
                             lambda self: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(RuntimeError, match="MQTT configuration is unreadable"):
            trauma_mod._mqtt_credentials()


class TestMedicalAICredentialsResolution:
    def test_service_entry_wins(self, monkeypatch):
        write_specter_json(monkeypatch, {
            "mqtt": {"services": {"medical_ai": {"username": "specter-medical-ai", "password": "svc-pass"}}}
        })
        assert medical_ai_mod._mqtt_credentials() == ("specter-medical-ai", "svc-pass")

    def test_rejects_missing_config(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(pathlib.Path, "read_text",
                             lambda self: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(RuntimeError, match="MQTT configuration is unreadable"):
            medical_ai_mod._mqtt_credentials()


class TestECGAICredentialsResolution:
    def test_service_entry_wins(self):
        config = {"mqtt": {"services": {"ecg_ai": {
            "username": "specter-ecg-ai", "password": "svc-pass"
        }}}}
        assert ecg_ai_mod._mqtt_credentials(config) == ("specter-ecg-ai", "svc-pass")

    @pytest.mark.parametrize("service", [{}, {"username": "specter-ecg-ai", "password": "specter-change-me"}])
    def test_missing_or_placeholder_fails_closed(self, service):
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            ecg_ai_mod._mqtt_credentials({"mqtt": {"services": {"ecg_ai": service}}})


class TestWardCredentialsResolution:
    def test_service_entry_wins(self, monkeypatch):
        write_specter_json(monkeypatch, {
            "mqtt": {"services": {
                "ward": {"username": "specter-ward", "password": "svc-pass"}
            }}
        })
        assert ward_mod._mqtt_credentials() == ("specter-ward", "svc-pass")

    def test_rejects_missing_config(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(
            pathlib.Path, "read_text",
            lambda self: (_ for _ in ()).throw(FileNotFoundError()),
        )
        with pytest.raises(RuntimeError, match="MQTT configuration is unreadable"):
            ward_mod._mqtt_credentials()


class TestMeshCredentialsResolution:
    def test_service_entry_wins(self, monkeypatch):
        write_specter_json(monkeypatch, {
            "mqtt": {"services": {"mesh": {"username": "specter-mesh", "password": "svc-pass"}}}
        })
        assert mesh_mod._mqtt_credentials() == ("specter-mesh", "svc-pass")

    def test_rejects_broad_operator_credential_when_service_entry_missing(self, monkeypatch):
        write_specter_json(monkeypatch, {
            "mqtt": {"username": "specter-operator", "password": "operator-pass"}
        })
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            mesh_mod._mqtt_credentials()

    def test_rejects_missing_config(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(pathlib.Path, "read_text",
                             lambda self: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(RuntimeError, match="MQTT configuration is unreadable"):
            mesh_mod._mqtt_credentials()


class TestTraumaMonitorUsesOperatorRole:
    """specter_trauma_monitor.py isn't a systemd service - it's an operator-
    invoked CLI viewer, so it deliberately shares the broad operator
    credential rather than getting its own ACL account."""

    def test_rejects_default_when_config_unreadable(self, monkeypatch):
        import pathlib
        monkeypatch.setattr(pathlib.Path, "read_text",
                             lambda self: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(RuntimeError, match="MQTT configuration is unreadable"):
            trauma_monitor_mod._mqtt_credentials()

    def test_uses_operator_credentials_from_config_file(self, tmp_path, monkeypatch):
        import pathlib
        content = '{"mqtt": {"username": "specter-operator", "password": "cpass"}}'
        monkeypatch.setattr(pathlib.Path, "read_text", lambda self: content)
        assert trauma_monitor_mod._mqtt_credentials() == ("specter-operator", "cpass")

    def test_failed_v2_connection_reports_reason_without_callback_crash(self, capsys):
        trauma_monitor_mod.on_connect(None, None, None, "Not authorized", None)
        assert "MQTT connect failed: Not authorized" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Wiring: username_pw_set() actually applied at construction time
# ---------------------------------------------------------------------------

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


class TestMeshRelayAuth:
    def test_applies_credentials_from_helper(self, monkeypatch):
        monkeypatch.setattr(mesh_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        relay = mesh_mod.MeshRelayService("broker-host", 1883)
        assert relay.mqtt.username == "tuser"
        assert relay.mqtt.password == "tpass"


class TestMedicalHubAuth:
    def test_applies_credentials_from_helper(self, monkeypatch):
        monkeypatch.setattr(medical_hub_mod, "_mqtt_credentials", lambda: ("tuser", "tpass"))
        hub = medical_hub_mod.MedicalHubBleCollector()
        assert hub.mqtt_client.username == "tuser"
        assert hub.mqtt_client.password == "tpass"


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


# ---------------------------------------------------------------------------
# __init__-time resolution for the 4 config-driven modules (run()/main()
# start blocking loops, so those aren't safely callable in a test)
# ---------------------------------------------------------------------------

class TestMqttCoordinatorAuthResolution:
    def test_service_entry_wins_over_flat_and_default(self, monkeypatch):
        monkeypatch.setattr(coordinator_mod, "load_config", lambda: {
            "mqtt": {
                "broker": "10.0.0.1", "port": 1883,
                "username": "specter-operator", "password": "operator-pass",
                "services": {"coordinator": {"username": "specter-coordinator", "password": "svc-pass"}},
            }
        })
        c = coordinator_mod.MQTTCoordinator()
        assert c.username == "specter-coordinator"
        assert c.password == "svc-pass"

    def test_rejects_flat_operator_fields(self, monkeypatch):
        monkeypatch.setattr(coordinator_mod, "load_config", lambda: {
            "mqtt": {"username": "specter-operator", "password": "operator-pass"}
        })
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            coordinator_mod.MQTTCoordinator()

    def test_rejects_missing_service_credentials(self, monkeypatch):
        monkeypatch.setattr(coordinator_mod, "load_config", lambda: {"mqtt": {}})
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            coordinator_mod.MQTTCoordinator()


class TestSdrControlAuthResolution:
    def test_service_entry_wins(self, monkeypatch):
        monkeypatch.setattr(sdr_mod, "load_config", lambda: {
            "mqtt": {"services": {"sdr_control": {"username": "specter-sdr-control", "password": "svc-pass"}}}
        })
        svc = sdr_mod.SDRControlService()
        assert svc.username == "specter-sdr-control"
        assert svc.password == "svc-pass"

    def test_rejects_missing_service_credentials(self, monkeypatch):
        monkeypatch.setattr(sdr_mod, "load_config", lambda: {})
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            sdr_mod.SDRControlService()


class TestThermalMonitorAuthResolution:
    def test_service_entry_wins(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "specter.json"
        cfg_file.write_text(json.dumps({
            "mqtt": {"services": {"thermal": {"username": "specter-thermal", "password": "svc-pass"}}}
        }))
        monkeypatch.setattr(thermal_mod, "CONFIG_PATH", cfg_file)
        monkeypatch.setattr(thermal_mod, "load_config", lambda: {})
        mon = thermal_mod.ThermalMonitor()
        assert mon.username == "specter-thermal"
        assert mon.password == "svc-pass"

    def test_rejects_missing_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(thermal_mod, "CONFIG_PATH", tmp_path / "does_not_exist.json")
        monkeypatch.setattr(thermal_mod, "load_config", lambda: {})
        with pytest.raises(RuntimeError, match="MQTT configuration is unreadable"):
            thermal_mod.ThermalMonitor()


class TestDashboardMQTTAuth:
    def test_missing_credentials_are_rejected(self):
        with pytest.raises(TypeError):
            dashboard_mod.DashboardMQTT(broker="192.168.1.1", port=1883)

    def test_placeholder_credentials_are_rejected(self):
        with pytest.raises(RuntimeError, match="dedicated MQTT credentials missing"):
            dashboard_mod.DashboardMQTT(
                broker="192.168.1.1", port=1883,
                username=dashboard_mod.MQTT_DEFAULT_USERNAME,
                password=dashboard_mod.MQTT_DEFAULT_PASSWORD,
            )

    def test_explicit_credentials_are_stored(self):
        m = dashboard_mod.DashboardMQTT(
            broker="192.168.1.1", port=1883, username="cuser", password="cpass")
        assert m.username == "cuser"
        assert m.password == "cpass"


# ---------------------------------------------------------------------------
# _mqtt_client() paho-mqtt 2.x API and 1.x compatibility fallback
#
# On paho-mqtt 1.x, mqtt.CallbackAPIVersion does not exist, so
# `mqtt.CallbackAPIVersion.VERSION2` raises AttributeError before Client()
# is even reached. The except branch used to call _mqtt_client() again
# instead of falling back to the old-style constructor - since the
# AttributeError is deterministic (the attribute either exists or it
# doesn't), every retry hit the exact same error, recursing until
# RecursionError, which could prevent trauma/medical services from
# starting at all on that paho-mqtt version. These simulate that
# environment by removing CallbackAPIVersion, without needing an actual
# paho-mqtt 1.x install.
# ---------------------------------------------------------------------------

class _Paho1xStubClient:
    """Stands in for paho-mqtt 1.x's Client(client_id=...) constructor."""
    def __init__(self, client_id=""):
        self.client_id = client_id


class _Paho1xStubModule:
    """
    Stands in for the `paho.mqtt.client` module as it looks on paho-mqtt
    1.x: Client() takes no callback_api_version argument, and
    CallbackAPIVersion does not exist at all - accessing it raises
    AttributeError, exactly like the real 1.x module does. Deleting
    CallbackAPIVersion from the real (2.x) module instead would break
    paho's own Client.__init__, which references that name internally -
    this stub avoids touching real paho internals at all.
    """
    Client = _Paho1xStubClient


class TestMqttClientCompatFallback:
    MODULES = [
        trauma_mod, trauma_monitor_mod, ward_mod, mesh_mod, medical_ai_mod,
        medical_hub_mod,
    ]

    @pytest.mark.parametrize("mod", MODULES, ids=lambda m: m.__name__)
    def test_returns_client_on_current_paho_version(self, mod):
        client = mod._mqtt_client("test-client-id")
        assert client.callback_api_version == paho_mqtt.CallbackAPIVersion.VERSION2

    @pytest.mark.parametrize("mod", MODULES, ids=lambda m: m.__name__)
    def test_falls_back_without_recursing_on_paho_1x(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "mqtt", _Paho1xStubModule())
        # Must return a real client via the old-style constructor, not
        # recurse into itself and blow the stack with RecursionError -
        # which is exactly what happened before this was fixed (the
        # except branch called _mqtt_client() again instead of falling
        # back to mqtt.Client(client_id=...), and the AttributeError from
        # a missing CallbackAPIVersion is deterministic, so every retry
        # hit the identical error).
        client = mod._mqtt_client("test-client-id")
        assert isinstance(client, _Paho1xStubClient)
        assert client.client_id == "test-client-id"

    @pytest.mark.parametrize(
        "mod", [coordinator_mod, sdr_mod, thermal_mod], ids=lambda m: m.__name__
    )
    def test_core_helpers_use_current_callback_api(self, mod):
        client = mod._mqtt_client(paho_mqtt, "test-client-id")
        assert client.callback_api_version == paho_mqtt.CallbackAPIVersion.VERSION2
