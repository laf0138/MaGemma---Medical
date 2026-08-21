"""
Tests for TraumaService._on_message (trauma/specter_trauma.py) - the MQTT
command router that turns `shtf/trauma/command/<cmd>` messages into
SceneRegistry mutations. Verified separately from SceneRegistry itself
(see test_trauma.py) because the routing/parsing layer has its own failure
modes: unknown commands, non-JSON payloads, and missing required fields
must degrade safely rather than crash the service or corrupt the scene.
"""
import json

import pytest

from trauma.specter_trauma import TraumaService


class FakeMQTT:
    """Records publish() calls instead of touching a real broker."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class FakeMsg:
    def __init__(self, topic: str, payload):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else payload.encode()


def command_msg(cmd: str, **payload) -> FakeMsg:
    return FakeMsg(f"shtf/trauma/command/{cmd}", json.dumps(payload))


@pytest.fixture
def service(tmp_path):
    svc = TraumaService("mqtt-host", 1883, str(tmp_path / "scene.json"))
    svc.mqtt = FakeMQTT()
    return svc


class TestSceneLifecycleCommands:
    def test_open_scene_activates_and_clears(self, service):
        service.registry.add_casualty()
        service._on_message(None, None, command_msg("open_scene"))
        assert service.registry.scene_active is True
        assert service.registry.casualties == {}

    def test_close_scene_deactivates(self, service):
        service._on_message(None, None, command_msg("open_scene"))
        service._on_message(None, None, command_msg("close_scene"))
        assert service.registry.scene_active is False

    def test_successful_command_republishes_scene(self, service):
        service._on_message(None, None, command_msg("open_scene"))
        assert any(topic == service.TOPIC_SCENE for topic, *_ in service.mqtt.published)


class TestCasualtyCommands:
    def test_add_casualty_creates_record_with_mechanism(self, service):
        service._on_message(None, None, command_msg("add_casualty", mechanism="gsw", notes="chest"))
        casualties = list(service.registry.casualties.values())
        assert len(casualties) == 1
        assert casualties[0].mechanism == "gsw"
        assert casualties[0].notes == "chest"

    def test_triage_command_sets_category(self, service):
        c = service.registry.add_casualty()
        service._on_message(None, None, command_msg("triage", casualty_id=c.casualty_id, category="IMMEDIATE"))
        assert service.registry.get(c.casualty_id).triage_category == "IMMEDIATE"

    def test_intervention_command_logs_tourniquet(self, service):
        c = service.registry.add_casualty()
        service._on_message(None, None, command_msg(
            "intervention", casualty_id=c.casualty_id, type="tourniquet", site="RLE"))
        updated = service.registry.get(c.casualty_id)
        assert len(updated.interventions) == 1
        assert len(updated.tourniquets) == 1

    def test_convert_tourniquet_command(self, service):
        c = service.registry.add_casualty()
        service.registry.log_intervention(c.casualty_id, "tourniquet", site="RLE")
        tq_id = service.registry.get(c.casualty_id).tourniquets[0].tq_id
        service._on_message(None, None, command_msg(
            "convert_tourniquet", casualty_id=c.casualty_id, tq_id=tq_id))
        assert service.registry.get(c.casualty_id).tourniquets[0].converted_utc is not None

    def test_vitals_command_appends_reading(self, service):
        c = service.registry.add_casualty()
        service._on_message(None, None, command_msg(
            "vitals", casualty_id=c.casualty_id, vitals={"pulse": 100, "bp_systolic": 110}))
        assert len(service.registry.get(c.casualty_id).vitals) == 1

    def test_assessed_command_updates_timestamp(self, service):
        c = service.registry.add_casualty()
        before = service.registry.get(c.casualty_id).last_assessed_utc
        service._on_message(None, None, command_msg("assessed", casualty_id=c.casualty_id))
        after = service.registry.get(c.casualty_id).last_assessed_utc
        assert after >= before

    def test_protocol_step_command_sets_state(self, service):
        c = service.registry.add_casualty()
        service._on_message(None, None, command_msg(
            "protocol_step", casualty_id=c.casualty_id, step_id="m2", done=True))
        assert service.registry.get(c.casualty_id).protocol_state.get("m2") is True


class TestMalformedInput:
    def test_unknown_command_does_not_raise_or_publish(self, service):
        before = len(service.mqtt.published)
        service._on_message(None, None, command_msg("frobnicate"))
        assert len(service.mqtt.published) == before

    def test_non_json_payload_does_not_raise(self, service):
        service._on_message(None, None, FakeMsg("shtf/trauma/command/add_casualty", b"not json"))
        assert service.registry.casualties == {}

    def test_command_missing_required_field_does_not_raise(self, service):
        # triage requires casualty_id in payload - omit it entirely.
        service._on_message(None, None, FakeMsg("shtf/trauma/command/triage", b"{}"))
        # No casualty existed and none should have been created/crash the service.
        assert service.registry.casualties == {}

    def test_missing_field_does_not_republish_scene(self, service):
        before = len(service.mqtt.published)
        service._on_message(None, None, FakeMsg("shtf/trauma/command/triage", b"{}"))
        assert len(service.mqtt.published) == before

    def test_command_against_nonexistent_casualty_does_not_raise(self, service):
        service._on_message(None, None, command_msg("triage", casualty_id="C-999", category="MINIMAL"))
        assert service.registry.get("C-999") is None

    def test_empty_payload_on_open_scene_is_fine(self, service):
        service._on_message(None, None, FakeMsg("shtf/trauma/command/open_scene", b""))
        assert service.registry.scene_active is True
