"""
Tests for WardService._on_message (ward/specter_ward.py) - the MQTT command
router that turns `shtf/ward/command/<cmd>` messages into
CareEpisodeRegistry mutations. Mirrors test_trauma_mqtt_dispatch.py:
verified separately from CareEpisodeRegistry itself (see test_ward.py)
because the routing/parsing layer has its own failure modes - unknown
commands, non-JSON payloads, and missing required fields must degrade
safely rather than crash the service or corrupt episode state.
"""
import json

import pytest

from ward.specter_ward import WardService


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
    return FakeMsg(f"shtf/ward/command/{cmd}", json.dumps(payload))


@pytest.fixture
def service(tmp_path):
    svc = WardService("mqtt-host", 1883, str(tmp_path / "ward.json"))
    svc.mqtt = FakeMQTT()
    return svc


class TestEpisodeLifecycleCommands:
    def test_open_episode_creates_active_episode(self, service):
        service._on_message(None, None, command_msg(
            "open_episode", patient_id="p1", presenting_problem="pneumonia"))
        episodes = service.registry.open_episodes()
        assert len(episodes) == 1
        assert episodes[0].patient_id == "p1"
        assert episodes[0].presenting_problem == "pneumonia"

    def test_close_episode_deactivates(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg("close_episode", episode_id=eid))
        assert service.registry.open_episodes() == []

    def test_successful_command_republishes_episode_topic(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        assert any(topic == service.TOPIC_EPISODE for topic, *_ in service.mqtt.published)


class TestFluidCommands:
    def test_intake_command_logs_entry(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg(
            "intake", episode_id=eid, route="oral", volume_ml=250, description="water"))
        ep = service.registry.get(eid)
        assert len(ep.fluid_intake) == 1
        assert ep.fluid_intake[0].volume_ml == 250

    def test_output_command_logs_entry(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg(
            "output", episode_id=eid, route="urine", volume_ml=300))
        ep = service.registry.get(eid)
        assert len(ep.fluid_output) == 1


class TestCareTaskCommands:
    def test_add_care_task_with_explicit_interval(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        before = len(service.registry.get(eid).care_tasks)
        service._on_message(None, None, command_msg(
            "add_care_task", episode_id=eid, task_type="medication",
            interval_minutes=360, label="prednisone"))
        after = service.registry.get(eid).care_tasks
        assert len(after) == before + 1
        assert after[-1].label == "prednisone"

    def test_complete_task_command_stamps_last_done(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        task_id = service.registry.get(eid).care_tasks[0].task_id
        service._on_message(None, None, command_msg(
            "complete_task", episode_id=eid, task_id=task_id))
        task = next(t for t in service.registry.get(eid).care_tasks if t.task_id == task_id)
        assert task.last_done_utc != ""


class TestClinicalRecordCommands:
    def test_skin_check_command(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg(
            "skin_check", episode_id=eid, sites={"sacrum": "intact"}))
        assert len(service.registry.get(eid).skin_checks) == 1

    def test_nutrition_command(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg(
            "nutrition", episode_id=eid, description="lunch", percent_consumed=60))
        assert len(service.registry.get(eid).nutrition) == 1

    def test_mobility_command(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg(
            "mobility", episode_id=eid, level="sat_edge", duration_minutes=5))
        assert len(service.registry.get(eid).mobility) == 1

    def test_vitals_command_computes_news2(self, service):
        service._on_message(None, None, command_msg("open_episode", patient_id="p1"))
        eid = service.registry.open_episodes()[0].episode_id
        service._on_message(None, None, command_msg(
            "vitals", episode_id=eid,
            values={"rr": 18, "spo2": 97, "bp_systolic": 120, "pulse": 75,
                    "avpu": "A", "temperature_c": 37.0}))
        ep = service.registry.get(eid)
        assert len(ep.vitals) == 1
        assert ep.vitals[0].news2["total"] == 0


class TestMalformedInput:
    def test_unknown_command_does_not_raise_or_publish(self, service):
        before = len(service.mqtt.published)
        service._on_message(None, None, command_msg("frobnicate"))
        assert len(service.mqtt.published) == before

    def test_non_json_payload_does_not_raise(self, service):
        service._on_message(None, None, FakeMsg("shtf/ward/command/open_episode", b"not json"))
        assert service.registry.open_episodes() == []

    def test_command_missing_required_field_does_not_raise(self, service):
        # intake requires episode_id/route/volume_ml - omit them entirely.
        service._on_message(None, None, FakeMsg("shtf/ward/command/intake", b"{}"))
        assert service.registry.episodes == {}

    def test_missing_field_does_not_republish(self, service):
        before = len(service.mqtt.published)
        service._on_message(None, None, FakeMsg("shtf/ward/command/intake", b"{}"))
        assert len(service.mqtt.published) == before

    def test_command_against_nonexistent_episode_does_not_raise(self, service):
        service._on_message(None, None, command_msg(
            "intake", episode_id="W-999", route="oral", volume_ml=100))
        assert service.registry.get("W-999") is None

    def test_empty_payload_on_open_episode_is_fine(self, service):
        # patient_id is a required field, so this is a KeyError caught
        # inside _on_message, not a crash - no episode should be created.
        service._on_message(None, None, FakeMsg("shtf/ward/command/open_episode", b""))
        assert service.registry.open_episodes() == []
