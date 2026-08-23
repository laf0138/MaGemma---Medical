"""
Tests for MedicalAIEngine (medical/specter_medical_ai.py) - MQTT vitals
ingest, dynamic patient profile overrides, query dispatch, and the query
pipeline itself (retrieval -> prompt -> Ollama -> publish diagnosis).

Ollama and ChromaDB calls are mocked throughout: this suite is checking
SPECTER's own routing/error-handling logic, not the model or vector store.
"""
import json

import pytest
import requests

import medical.specter_medical_ai as medical_ai_mod
from medical.specter_medical_ai import Config, MedicalAIEngine


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class SyncThread:
    """Runs the Thread target immediately instead of on a background thread,
    so _handle_query's dispatch can be asserted deterministically."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(medical_ai_mod, "_mqtt_credentials", lambda: ("test-user", "test-password"))
    cfg = Config(chroma_path="/does/not/exist")
    eng = MedicalAIEngine(cfg)
    eng.mqtt = FakeMQTT()
    monkeypatch.setattr(eng.ollama, "model_present", lambda: False)
    return eng


def last_diagnosis(engine) -> dict:
    """The most recent shtf/medical/diagnosis/<patient> publish, decoded.

    Not published[-1]: _run_query's `finally` block republishes AI status
    (shtf/medical/ai/status) after the diagnosis, so the diagnosis message
    is not necessarily the last thing on the wire.
    """
    calls = [c for c in engine.mqtt.published if c[0].startswith("shtf/medical/diagnosis/")]
    return json.loads(calls[-1][1])


def msg_json(topic: str, payload: dict):
    class Msg:
        pass
    m = Msg()
    m.topic = topic
    m.payload = json.dumps(payload).encode()
    return m


# ---------------------------------------------------------------------------
# Vitals ingest
# ---------------------------------------------------------------------------

class TestHandleVitals:
    def test_snapshot_shape_stores_all_readings(self, engine):
        engine._handle_vitals("shtf/medical/vitals/operator", {
            "readings": [
                {"reading_type": "pulse", "value": 72, "unit": "bpm"},
                {"reading_type": "spo2", "value": 98, "unit": "%"},
            ]
        })
        latest = engine.vitals.latest("operator")
        assert set(latest.keys()) == {"pulse", "spo2"}
        assert latest["pulse"].value == 72

    def test_single_reading_shape_stores_under_topic_reading_type(self, engine):
        engine._handle_vitals("shtf/medical/vitals/operator/pulse", {
            "value": 88, "unit": "bpm",
        })
        assert engine.vitals.latest("operator")["pulse"].value == 88

    def test_short_topic_is_ignored(self, engine):
        engine._handle_vitals("shtf/medical/vitals", {"value": 1})
        assert engine.vitals.latest("operator") == {}

    def test_reading_missing_type_is_dropped(self, engine):
        engine._handle_vitals("shtf/medical/vitals/operator", {
            "readings": [{"value": 72, "unit": "bpm"}],  # no reading_type
        })
        assert engine.vitals.latest("operator") == {}

    def test_vitals_snapshot_triggers_derived_metrics_publish(self, engine):
        engine._handle_vitals("shtf/medical/vitals/operator", {
            "readings": [
                {"reading_type": "bp_systolic", "value": 120, "unit": "mmHg"},
                {"reading_type": "bp_diastolic", "value": 80, "unit": "mmHg"},
            ]
        })
        derived_calls = [
            c for c in engine.mqtt.published
            if c[0] == "shtf/medical/derived/operator"
        ]
        assert len(derived_calls) == 1
        topic, payload, qos, retain = derived_calls[0]
        body = json.loads(payload)
        assert body["map_mmhg"] == pytest.approx(93.3)
        assert body["patient_id"] == "operator"
        assert retain is True

    def test_short_topic_does_not_publish_derived_metrics(self, engine):
        engine._handle_vitals("shtf/medical/vitals", {"value": 1})
        assert not any(
            c[0].startswith("shtf/medical/derived/") for c in engine.mqtt.published
        )

    def test_ecg_analysis_readings_flow_through_to_the_built_prompt(self, engine):
        # A realistic snapshot payload shaped like what medical_hub.py's
        # _collect_polar_h10_stream + ecg_analysis.py actually publish -
        # full round trip: MQTT payload -> VitalsCache -> PromptBuilder.
        engine._handle_vitals("shtf/medical/vitals/operator", {
            "readings": [
                {"reading_type": "ecg_waveform_uv", "value": list(range(500)), "unit": "uV"},
                {"reading_type": "ecg_q_s_peak_interval_ms", "value": 84.6, "unit": "ms"},
                {"reading_type": "ecg_t_r_abs_ratio", "value": 0.8, "unit": "ratio"},
                {
                    "reading_type": "ecg_analysis_status",
                    "value": "experimental_not_clinically_validated",
                    "unit": "text",
                },
            ]
        })

        prompt = engine.prompts.build(
            patient_id="operator", user_query="how is the patient doing",
            passages=[], retriever=engine.retriever,
        )

        assert "ECG Q-to-S peak interval (experimental; not QRS duration): 84.6 ms" in prompt
        assert "ECG absolute T/R amplitude ratio (experimental): 0.8 ratio" in prompt
        assert "experimental_not_clinically_validated" in prompt
        # The raw waveform must never be dumped into the text prompt.
        assert "ecg_waveform_uv" not in prompt
        assert str(list(range(500))) not in prompt


# ---------------------------------------------------------------------------
# Profile overrides
# ---------------------------------------------------------------------------

class TestHandleProfile:
    def test_new_patient_profile_is_created(self, engine):
        engine._handle_profile("shtf/medical/profile/kid", {
            "display_name": "Kid", "age": 8, "conditions": ["Asthma"],
        })
        prof = engine.profiles["kid"]
        assert prof.display_name == "Kid"
        assert prof.age == 8
        assert prof.conditions == ["Asthma"]

    def test_partial_update_preserves_unset_fields_on_existing_profile(self, engine):
        before = engine.profiles["operator"].conditions
        engine._handle_profile("shtf/medical/profile/operator", {"display_name": "Op"})
        after = engine.profiles["operator"]
        assert after.display_name == "Op"
        assert after.conditions == before  # untouched

    def test_prompt_builder_sees_updated_profile(self, engine):
        engine._handle_profile("shtf/medical/profile/kid", {"display_name": "Kid"})
        assert engine.prompts.profile_for("kid").display_name == "Kid"

    def test_short_topic_is_ignored(self, engine):
        before = dict(engine.profiles)
        engine._handle_profile("shtf/medical/profile", {"display_name": "X"})
        assert engine.profiles.keys() == before.keys()


# ---------------------------------------------------------------------------
# _on_message dispatch
# ---------------------------------------------------------------------------

class TestOnMessageDispatch:
    def test_vitals_topic_routes_to_handle_vitals(self, engine):
        engine._on_message(None, None, msg_json(
            "shtf/medical/vitals/operator/pulse", {"value": 70, "unit": "bpm"}))
        assert engine.vitals.latest("operator")["pulse"].value == 70

    def test_profile_topic_routes_to_handle_profile(self, engine):
        engine._on_message(None, None, msg_json(
            "shtf/medical/profile/kid", {"display_name": "Kid"}))
        assert engine.profiles["kid"].display_name == "Kid"

    def test_non_json_payload_does_not_raise(self, engine):
        class Msg:
            topic = "shtf/medical/vitals/operator"
            payload = b"not json"
        engine._on_message(None, None, Msg())
        assert engine.vitals.latest("operator") == {}


# ---------------------------------------------------------------------------
# Query dispatch (_handle_query)
# ---------------------------------------------------------------------------

class TestHandleQueryDispatch:
    def test_valid_query_invokes_run_query_with_parsed_args(self, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_run_query", lambda pid, q, rid: calls.append((pid, q, rid)))
        monkeypatch.setattr(
            "medical.specter_medical_ai.threading.Thread", SyncThread)
        engine._handle_query("shtf/medical/query/operator", {"query": "fever?"})
        assert len(calls) == 1
        assert calls[0][0] == "operator"
        assert calls[0][1] == "fever?"

    def test_empty_question_does_not_invoke_run_query(self, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_run_query", lambda pid, q, rid: calls.append(1))
        monkeypatch.setattr(
            "medical.specter_medical_ai.threading.Thread", SyncThread)
        engine._handle_query("shtf/medical/query/operator", {"query": "   "})
        assert calls == []

    def test_missing_patient_in_topic_defaults_to_default(self, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_run_query", lambda pid, q, rid: calls.append(pid))
        monkeypatch.setattr(
            "medical.specter_medical_ai.threading.Thread", SyncThread)
        engine._handle_query("shtf/medical/query", {"query": "fever?"})
        assert calls == ["default"]


# ---------------------------------------------------------------------------
# _run_query pipeline (retrieval -> prompt -> ollama -> publish)
# ---------------------------------------------------------------------------

class TestRunQueryPipeline:
    def test_successful_query_publishes_diagnosis(self, engine, monkeypatch):
        monkeypatch.setattr(engine.retriever, "retrieve",
                             lambda q, n_results=4: [{"text": "guidance", "source": "WHO", "page": 1}])
        monkeypatch.setattr(engine.ollama, "generate", lambda prompt: "Assessment: likely viral.")

        engine._run_query("operator", "fever and cough", "req-1")

        diagnosis_calls = [c for c in engine.mqtt.published if c[0] == "shtf/medical/diagnosis/operator"]
        assert len(diagnosis_calls) == 1
        payload = json.loads(diagnosis_calls[0][1])
        assert payload["answer"] == "Assessment: likely viral."
        assert payload["request_id"] == "req-1"
        assert payload["error"] == ""
        assert payload["sources"] == [{"source": "WHO", "page": 1}]
        assert "Not a substitute" in payload["disclaimer"]

    def test_busy_lock_released_after_success(self, engine, monkeypatch):
        monkeypatch.setattr(engine.retriever, "retrieve", lambda q, n_results=4: [])
        monkeypatch.setattr(engine.ollama, "generate", lambda prompt: "ok")
        engine._run_query("operator", "q", "req-1")
        assert engine._busy.acquire(blocking=False) is True
        engine._busy.release()

    def test_concurrent_query_returns_busy_error_without_calling_model(self, engine, monkeypatch):
        called = []
        monkeypatch.setattr(engine.ollama, "generate", lambda prompt: called.append(1))
        engine._busy.acquire()
        try:
            engine._run_query("operator", "q", "req-2")
        finally:
            engine._busy.release()

        payload = json.loads(engine.mqtt.published[-1][1])
        assert "already in progress" in payload["error"]
        assert called == []

    def test_timeout_is_reported_as_error_and_lock_released(self, engine, monkeypatch):
        monkeypatch.setattr(engine.retriever, "retrieve", lambda q, n_results=4: [])

        def raise_timeout(prompt):
            raise requests.exceptions.Timeout()
        monkeypatch.setattr(engine.ollama, "generate", raise_timeout)

        engine._run_query("operator", "q", "req-3")

        payload = last_diagnosis(engine)
        assert "timed out" in payload["error"].lower()
        assert engine._busy.acquire(blocking=False) is True
        engine._busy.release()

    def test_unexpected_exception_is_reported_as_error_and_lock_released(self, engine, monkeypatch):
        monkeypatch.setattr(engine.retriever, "retrieve", lambda q, n_results=4: [])

        def boom(prompt):
            raise ValueError("model exploded")
        monkeypatch.setattr(engine.ollama, "generate", boom)

        engine._run_query("operator", "q", "req-4")

        payload = last_diagnosis(engine)
        assert "model exploded" in payload["error"]
        assert engine._busy.acquire(blocking=False) is True
        engine._busy.release()


class TestRetrievalQueryBias:
    def test_biases_toward_top_two_standing_conditions(self, engine):
        query = engine._retrieval_query("operator", "fever")
        assert "fever" in query
        assert "Kidney transplant recipient" in query
        assert "Chronic immunosuppression" in query

    def test_no_conditions_leaves_query_unchanged(self, engine):
        engine.profiles["stranger"] = engine.prompts.profile_for("stranger")
        query = engine._retrieval_query("stranger", "fever")
        assert query == "fever"


class TestIncludeSourcingFlag:
    def test_cost_related_question_sets_include_sourcing_true(self, engine, monkeypatch):
        captured = {}
        real_build = engine.prompts.build

        def spy(**kwargs):
            captured.update(kwargs)
            return real_build(**kwargs)

        monkeypatch.setattr(engine.prompts, "build", spy)
        monkeypatch.setattr(engine.retriever, "retrieve", lambda q, n_results=4: [])
        monkeypatch.setattr(engine.ollama, "generate", lambda p: "ok")

        engine._run_query("operator", "how much does this medication cost", "req-5")
        assert captured["include_sourcing"] is True

    def test_clinical_question_sets_include_sourcing_false(self, engine, monkeypatch):
        captured = {}
        real_build = engine.prompts.build

        def spy(**kwargs):
            captured.update(kwargs)
            return real_build(**kwargs)

        monkeypatch.setattr(engine.prompts, "build", spy)
        monkeypatch.setattr(engine.retriever, "retrieve", lambda q, n_results=4: [])
        monkeypatch.setattr(engine.ollama, "generate", lambda p: "ok")

        engine._run_query("operator", "is this a fever", "req-6")
        assert captured["include_sourcing"] is False
