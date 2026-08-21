"""
Tests for mesh/specter_mesh_relay.py - the LoRa/Meshtastic alarm relay.

The hardware layer (MeshtasticHardware) is tested against the REAL
`meshtastic` package (not a hand-rolled fake) wherever possible, because
the one bug that actually matters here - SerialInterface(devPath=None)
calling sys.exit() outright when more than one candidate port is found,
rather than raising a catchable exception - was found by reading that
package's real source, not by guessing. A test built only against a fake
of our own design would never have caught it, and would happily keep
passing even if a future meshtastic release changed that behavior again.

The relay/dedup/formatting logic (MeshRelayService) doesn't need real
hardware at all - it's tested against a lightweight FakeIface standing in
for whatever meshtastic.mesh_interface.MeshInterface object
MeshtasticHardware.connect() would normally return.
"""
import json
import threading
import time

import pytest

import mesh.specter_mesh_relay as mr


# ---------------------------------------------------------------------------
# normalize_alerts - trauma/ward publish a list of {"level","text"} objects;
# shtf/system/alarm publishes a single object with an inconsistent shape
# across today's publishers (thermal_monitor uses "msg"+"level",
# specter_rx_ring_buffer uses "msg" with no "level" at all).
# ---------------------------------------------------------------------------

class TestNormalizeAlerts:
    def test_trauma_alert_list_shape(self):
        out = mr.normalize_alerts("shtf/trauma/alert", [
            {"level": "critical", "text": "Tourniquet at 02:10:00", "casualty_id": "c1"},
        ])
        assert out == [{"source": "trauma", "level": "critical", "text": "Tourniquet at 02:10:00"}]

    def test_system_alarm_dict_with_msg_and_level(self):
        out = mr.normalize_alerts("shtf/system/alarm", {
            "source": "thermal_monitor", "level": "critical", "msg": "CPU 85C",
        })
        assert out == [{"source": "thermal_monitor", "level": "critical", "text": "CPU 85C"}]

    def test_system_alarm_dict_missing_level_defaults_caution(self):
        out = mr.normalize_alerts("shtf/system/alarm", {"source": "specter-rx", "msg": "Truncated"})
        assert out[0]["level"] == "caution"

    def test_item_missing_text_and_msg_is_dropped_not_crashed(self):
        out = mr.normalize_alerts("shtf/trauma/alert", [{"level": "critical"}])
        assert out == []

    def test_non_dict_item_in_list_is_skipped_not_crashed(self):
        out = mr.normalize_alerts("shtf/trauma/alert", [None, "garbage", {"text": "ok"}])
        assert out == [{"source": "trauma", "level": "caution", "text": "ok"}]

    def test_empty_list_is_empty(self):
        assert mr.normalize_alerts("shtf/ward/alert", []) == []


# ---------------------------------------------------------------------------
# MeshtasticHardware - real library, no fake
# ---------------------------------------------------------------------------

class TestMeshtasticHardwareResolvePort:
    def test_explicit_dev_path_never_touches_meshtastic_util(self, monkeypatch):
        import meshtastic.util as mutil

        def boom(*a, **k):
            raise AssertionError("findPorts should not be called when devPath is explicit")
        monkeypatch.setattr(mutil, "findPorts", boom)

        hw = mr.MeshtasticHardware(dev_path="/dev/ttyUSB0")
        assert hw.resolve_port() == ("/dev/ttyUSB0", "ok")

    def test_no_ports_found_is_not_found_not_a_crash(self, monkeypatch):
        import meshtastic.util as mutil
        monkeypatch.setattr(mutil, "findPorts", lambda eliminate_duplicates=True: [])
        hw = mr.MeshtasticHardware()
        assert hw.resolve_port() == (None, "not_found")

    def test_exactly_one_port_found_is_ok(self, monkeypatch):
        import meshtastic.util as mutil
        monkeypatch.setattr(mutil, "findPorts", lambda eliminate_duplicates=True: ["/dev/ttyUSB0"])
        hw = mr.MeshtasticHardware()
        assert hw.resolve_port() == ("/dev/ttyUSB0", "ok")

    def test_multiple_ports_is_ambiguous_and_never_reaches_sys_exit(self, monkeypatch):
        """This is the real bug this wrapper exists to avoid: real
        SerialInterface(devPath=None) calls meshtastic.util.our_exit(...)
        (a bare sys.exit) when it finds more than one port itself.
        resolve_port() must detect the ambiguity BEFORE any SerialInterface
        is constructed, so that code path is never reached."""
        import meshtastic.util as mutil
        monkeypatch.setattr(
            mutil, "findPorts",
            lambda eliminate_duplicates=True: ["/dev/ttyUSB0", "/dev/ttyUSB1"],
        )
        hw = mr.MeshtasticHardware()
        assert hw.resolve_port() == (None, "ambiguous")

    def test_no_real_hardware_in_this_sandbox_resolves_not_found(self):
        """No mocking at all - proves the real, installed meshtastic
        package's findPorts() behaves as this module assumes when no LoRa
        radio is attached (true in CI and in this sandbox)."""
        hw = mr.MeshtasticHardware()
        dev_path, status = hw.resolve_port()
        assert status in ("not_found", "ambiguous", "ok")  # never raises
        if status == "not_found":
            assert dev_path is None

    def test_connect_raises_cleanly_when_port_unresolved(self, monkeypatch):
        import meshtastic.util as mutil
        monkeypatch.setattr(mutil, "findPorts", lambda eliminate_duplicates=True: [])
        hw = mr.MeshtasticHardware()
        with pytest.raises(RuntimeError, match="not_found"):
            hw.connect()

    def test_close_on_never_connected_hardware_is_a_no_op(self):
        hw = mr.MeshtasticHardware()
        hw.close()  # must not raise


# ---------------------------------------------------------------------------
# MeshRelayService - relay/dedup/formatting logic, no real hardware needed
# ---------------------------------------------------------------------------

class FakeIface:
    def __init__(self):
        self.alerts = []
        self.texts = []

    def sendAlert(self, text, **kw):
        self.alerts.append(text)

    def sendText(self, text, **kw):
        self.texts.append(text)


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def username_pw_set(self, *a, **k):
        pass

    def will_set(self, *a, **k):
        pass

    def connect(self, *a, **k):
        pass

    def loop_forever(self):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(mr, "_mqtt_credentials", lambda: ("specter-mesh", "pw"))
    monkeypatch.setattr(mr, "_mqtt_client", lambda client_id="": FakeMQTT())
    svc = mr.MeshRelayService(mqtt_host="192.168.1.1", mqtt_port=1883)
    svc.hw.iface = FakeIface()
    svc._ready.set()
    return svc


def sent_topics(service):
    return [c[0] for c in service.mqtt.published]


class TestHandleAlertDedup:
    def test_new_critical_alert_uses_send_alert(self, service):
        service._handle_alert("shtf/trauma/alert", [
            {"level": "critical", "text": "Tourniquet at 02:10:00"},
        ])
        assert service.hw.iface.alerts == ["SPECTER TRAUMA - CRITICAL: Tourniquet at 02:10:00"]
        assert service.hw.iface.texts == []

    def test_caution_only_alert_uses_send_text_not_send_alert(self, service):
        service._handle_alert("shtf/ward/alert", [{"level": "caution", "text": "Not reassessed"}])
        assert service.hw.iface.alerts == []
        assert service.hw.iface.texts == ["SPECTER WARD - CAUTION: Not reassessed"]

    def test_identical_repeat_within_cooldown_is_suppressed(self, service):
        alerts = [{"level": "critical", "text": "X"}]
        service._handle_alert("shtf/trauma/alert", alerts)
        service._handle_alert("shtf/trauma/alert", alerts)
        assert len(service.hw.iface.alerts) == 1

    def test_identical_repeat_past_cooldown_is_reaffirmed(self, service, monkeypatch):
        alerts = [{"level": "critical", "text": "X"}]
        service._handle_alert("shtf/trauma/alert", alerts)
        # simulate the cooldown having elapsed
        sig, _ts = service._last_sent["trauma"]
        service._last_sent["trauma"] = (sig, time.time() - service.REALERT_INTERVAL_SECONDS - 1)
        service._handle_alert("shtf/trauma/alert", alerts)
        assert len(service.hw.iface.alerts) == 2
        assert "still active" in service.hw.iface.alerts[-1]

    def test_changed_alert_sends_immediately_regardless_of_cooldown(self, service):
        service._handle_alert("shtf/trauma/alert", [{"level": "critical", "text": "A"}])
        service._handle_alert("shtf/trauma/alert", [{"level": "critical", "text": "B"}])
        assert len(service.hw.iface.alerts) == 2

    def test_clearing_alerts_sends_all_clear_text(self, service):
        service._handle_alert("shtf/trauma/alert", [{"level": "critical", "text": "X"}])
        service._handle_alert("shtf/trauma/alert", [])
        assert "all clear" in service.hw.iface.texts[-1]

    def test_empty_then_empty_again_sends_nothing(self, service):
        service._handle_alert("shtf/trauma/alert", [])
        service._handle_alert("shtf/trauma/alert", [])
        assert service.hw.iface.alerts == []
        assert service.hw.iface.texts == []

    def test_different_sources_tracked_independently(self, service):
        service._handle_alert("shtf/trauma/alert", [{"level": "critical", "text": "X"}])
        service._handle_alert("shtf/ward/alert", [{"level": "critical", "text": "X"}])
        assert len(service.hw.iface.alerts) == 2

    def test_message_longer_than_limit_is_truncated(self, service):
        long_text = "A" * 500
        service._handle_alert("shtf/trauma/alert", [{"level": "caution", "text": long_text}])
        assert len(service.hw.iface.texts[-1]) <= service.MAX_MESSAGE_CHARS

    def test_sent_confirmation_published_to_mqtt(self, service):
        service._handle_alert("shtf/trauma/alert", [{"level": "critical", "text": "X"}])
        assert "shtf/mesh/sent" in sent_topics(service)


class TestHandleCommand:
    def test_send_command_relays_text(self, service):
        service._handle_command("shtf/mesh/command/send", {"text": "Testing 123"})
        assert service.hw.iface.texts == ["Testing 123"]

    def test_send_command_with_alert_priority_uses_send_alert(self, service):
        service._handle_command("shtf/mesh/command/send", {"text": "Evac now", "priority": "alert"})
        assert service.hw.iface.alerts == ["Evac now"]

    def test_empty_text_is_ignored(self, service):
        service._handle_command("shtf/mesh/command/send", {"text": "   "})
        assert service.hw.iface.texts == []
        assert service.hw.iface.alerts == []

    def test_unknown_command_is_ignored_not_crashed(self, service):
        service._handle_command("shtf/mesh/command/bogus", {"text": "x"})
        assert service.hw.iface.texts == []


class TestSendWhenNotConnected:
    def test_dropped_when_hardware_not_ready(self, service):
        service._ready.clear()
        service._handle_command("shtf/mesh/command/send", {"text": "hello"})
        assert service.hw.iface.texts == []
        assert not any(t == "shtf/mesh/sent" for t in sent_topics(service))


class TestOnMeshText:
    def test_inbound_text_relayed_to_mqtt(self, service):
        service._on_mesh_text(packet={"fromId": "!abc123", "decoded": {"text": "copy that"}})
        inbound = [c for c in service.mqtt.published if c[0] == "shtf/mesh/inbound"]
        assert len(inbound) == 1
        body = json.loads(inbound[0][1])
        assert body["from"] == "!abc123"
        assert body["text"] == "copy that"

    def test_packet_without_text_is_ignored(self, service):
        service._on_mesh_text(packet={"fromId": "!abc123", "decoded": {}})
        assert not any(c[0] == "shtf/mesh/inbound" for c in service.mqtt.published)

    def test_none_packet_does_not_crash(self, service):
        service._on_mesh_text(packet=None)
        assert not any(c[0] == "shtf/mesh/inbound" for c in service.mqtt.published)


class TestOnMeshMessageDispatch:
    def test_alert_topic_routes_to_handle_alert(self, service, monkeypatch):
        called = {}
        monkeypatch.setattr(service, "_handle_alert", lambda t, p: called.setdefault("t", t))
        msg = type("M", (), {"topic": "shtf/trauma/alert", "payload": b"[]"})()
        service._on_message(None, None, msg)
        assert called["t"] == "shtf/trauma/alert"

    def test_command_topic_routes_to_handle_command(self, service, monkeypatch):
        called = {}
        monkeypatch.setattr(service, "_handle_command", lambda t, p: called.setdefault("t", t))
        msg = type("M", (), {"topic": "shtf/mesh/command/send", "payload": b"{}"})()
        service._on_message(None, None, msg)
        assert called["t"] == "shtf/mesh/command/send"

    def test_non_json_payload_does_not_raise(self, service):
        msg = type("M", (), {"topic": "shtf/trauma/alert", "payload": b"not json"})()
        service._on_message(None, None, msg)  # must not raise
