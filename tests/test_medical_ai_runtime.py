"""Hardware-free tests for Medical AI external boundaries and lifecycle."""

import json
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import pytest
import requests

import medical.specter_medical_ai as ai_mod
from medical.specter_medical_ai import (
    Config,
    GuidelineRetriever,
    MedicalAIEngine,
    OllamaClient,
    VitalReading,
    VitalsCache,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, raise_error=None):
        self.status_code = status_code
        self.body = body if body is not None else {}
        self.raise_error = raise_error

    def raise_for_status(self):
        if self.raise_error:
            raise self.raise_error

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class LifecycleMqtt:
    def __init__(self):
        self.calls = []
        self.published = []

    def subscribe(self, topic, qos=0):
        self.calls.append(("subscribe", topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def will_set(self, topic, payload, qos=0, retain=False):
        self.calls.append(("will_set", topic, payload, qos, retain))

    def connect(self, host, port, keepalive):
        self.calls.append(("connect", host, port, keepalive))

    def loop_forever(self):
        self.calls.append(("loop_forever",))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(ai_mod, "_mqtt_credentials", lambda: ("user", "secret"))
    value = MedicalAIEngine(Config(chroma_path="/missing"))
    value.mqtt = LifecycleMqtt()
    monkeypatch.setattr(value.ollama, "model_present", lambda: False)
    return value


class TestCredentials:
    def test_mqtt_1x_constructor_fallback(self, monkeypatch):
        sentinel = object()

        def client_constructor(*args, **kwargs):
            if args:
                raise TypeError("2.x callback API unsupported")
            assert kwargs["client_id"] == "medical-ai"
            return sentinel

        monkeypatch.setattr(ai_mod.mqtt, "Client", client_constructor)
        assert ai_mod._mqtt_client("medical-ai") is sentinel

    def test_dedicated_credentials_are_loaded(self, monkeypatch):
        body = {"mqtt": {"services": {"medical_ai": {
            "username": "ai-user", "password": "secret"
        }}}}
        monkeypatch.setattr(ai_mod.Path, "read_text", lambda self: json.dumps(body))
        assert ai_mod._mqtt_credentials() == ("ai-user", "secret")

    @pytest.mark.parametrize(
        "body",
        [
            "[]",
            "not-json",
            json.dumps({"mqtt": {"services": {"medical_ai": {}}}}),
            json.dumps({"mqtt": {"services": {"medical_ai": {
                "username": "ai", "password": ai_mod.MQTT_DEFAULT_PASSWORD
            }}}}),
        ],
    )
    def test_credentials_fail_closed(self, monkeypatch, body):
        monkeypatch.setattr(ai_mod.Path, "read_text", lambda self: body)
        with pytest.raises(RuntimeError):
            ai_mod._mqtt_credentials()


class TestOllamaClient:
    def test_health_success_non_200_and_timeout(self, monkeypatch):
        client = OllamaClient(Config())
        monkeypatch.setattr(ai_mod.requests, "get", lambda *a, **k: FakeResponse(200))
        assert client.health() is True
        monkeypatch.setattr(ai_mod.requests, "get", lambda *a, **k: FakeResponse(503))
        assert client.health() is False
        monkeypatch.setattr(
            ai_mod.requests,
            "get",
            lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()),
        )
        assert client.health() is False

    def test_model_presence_accepts_tag_variant_but_not_prefix_collision(self, monkeypatch):
        client = OllamaClient(Config(model="medgemma:4b"))
        responses = iter([
            FakeResponse(body={"models": [{"name": "medgemma:latest"}]}),
            FakeResponse(body={"models": [{"name": "medgemma2:4b"}]}),
        ])
        monkeypatch.setattr(ai_mod.requests, "get", lambda *a, **k: next(responses))
        assert client.model_present() is True
        assert client.model_present() is False

    @pytest.mark.parametrize(
        "response",
        [
            FakeResponse(500, raise_error=requests.HTTPError("bad status")),
            FakeResponse(body=ValueError("malformed JSON")),
        ],
    )
    def test_model_presence_failures_return_false(self, monkeypatch, response):
        monkeypatch.setattr(ai_mod.requests, "get", lambda *a, **k: response)
        assert OllamaClient(Config()).model_present() is False

    def test_generate_posts_bounded_options_and_returns_trimmed_text(self, monkeypatch):
        calls = []

        def post(url, json, timeout):
            calls.append((url, json, timeout))
            return FakeResponse(body={"response": "  assessment  ", "eval_count": 20})

        times = iter([100.0, 102.0])
        monkeypatch.setattr(ai_mod.requests, "post", post)
        monkeypatch.setattr(ai_mod.time, "time", lambda: next(times))
        cfg = Config(model="medgemma:4b", temperature=0.2, max_tokens=321, request_timeout=9)
        assert OllamaClient(cfg).generate("patient prompt") == "assessment"
        assert calls[0][1]["stream"] is False
        assert calls[0][1]["options"] == {"temperature": 0.2, "num_predict": 321}
        assert calls[0][2] == 9

    def test_generate_timeout_and_malformed_json_propagate(self, monkeypatch):
        client = OllamaClient(Config())
        monkeypatch.setattr(
            ai_mod.requests,
            "post",
            lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()),
        )
        with pytest.raises(requests.Timeout):
            client.generate("prompt")
        monkeypatch.setattr(
            ai_mod.requests,
            "post",
            lambda *a, **k: FakeResponse(body=ValueError("malformed JSON")),
        )
        with pytest.raises(ValueError, match="malformed JSON"):
            client.generate("prompt")


class TestGuidelineRetrieval:
    def test_retrieval_maps_results_and_missing_metadata(self):
        retriever = object.__new__(GuidelineRetriever)
        retriever.cfg = Config()
        retriever.collection = SimpleNamespace(
            query=lambda **kwargs: {
                "documents": [["first", "second"]],
                "metadatas": [[{"source": "WHO", "page": 4}, None]],
            }
        )
        assert retriever.retrieve("fever", n_results=2) == [
            {"text": "first", "source": "WHO", "page": 4},
            {"text": "second", "source": "unknown source", "page": None},
        ]

    def test_retrieval_failure_and_disabled_collection_return_empty(self):
        retriever = object.__new__(GuidelineRetriever)
        retriever.cfg = Config()
        retriever.collection = None
        assert retriever.retrieve("q") == []
        retriever.collection = SimpleNamespace(
            query=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("index corrupt"))
        )
        assert retriever.retrieve("q") == []

    def test_chroma_success_path_uses_persistent_collection(self, monkeypatch):
        collection = SimpleNamespace(count=lambda: 12)
        client = SimpleNamespace(
            get_or_create_collection=lambda name: collection
        )
        chromadb = ModuleType("chromadb")
        chromadb.PersistentClient = lambda path, settings: client
        chromadb_config = ModuleType("chromadb.config")
        chromadb_config.Settings = lambda anonymized_telemetry: SimpleNamespace(
            anonymized_telemetry=anonymized_telemetry
        )
        monkeypatch.setitem(sys.modules, "chromadb", chromadb)
        monkeypatch.setitem(sys.modules, "chromadb.config", chromadb_config)
        retriever = GuidelineRetriever(Config(chroma_path="/tmp/chroma"))
        assert retriever.collection is collection


class TestMqttAndEngineLifecycle:
    def test_connect_success_subscribes_and_publishes_online(self, engine):
        engine._on_connect(engine.mqtt, None, None, 0)
        assert engine.connected is True
        assert [call[1] for call in engine.mqtt.calls if call[0] == "subscribe"] == [
            engine.TOPIC_VITALS,
            engine.TOPIC_QUERY,
            engine.TOPIC_PROFILE,
            engine.TOPIC_ECG_ANALYSIS,
        ]
        status = json.loads(engine.mqtt.published[-1][1])
        assert status["state"] == "online"

    def test_connect_failure_and_both_disconnect_signatures(self, engine):
        engine._on_connect(engine.mqtt, None, None, 5)
        assert engine.connected is False
        engine.connected = True
        engine._on_disconnect(engine.mqtt, None, 7)
        assert engine.connected is False
        engine.connected = True
        engine._on_disconnect(engine.mqtt, None, object(), 8)
        assert engine.connected is False

    def test_query_message_dispatch_and_unknown_topic(self, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_handle_query", lambda topic, body: calls.append((topic, body)))
        msg = SimpleNamespace(
            topic="shtf/medical/query/operator",
            payload=json.dumps({"query": "fever"}).encode(),
        )
        engine._on_message(None, None, msg)
        assert calls == [(msg.topic, {"query": "fever"})]
        msg.topic = "shtf/other/topic"
        engine._on_message(None, None, msg)
        assert len(calls) == 1

    def test_snapshot_topic_without_readings_is_ignored(self, engine):
        engine._handle_vitals("shtf/medical/vitals/operator", {})
        assert engine.vitals.latest("operator") == {}
        assert engine.mqtt.published == []

    @pytest.mark.parametrize("health,present", [(False, False), (True, False), (True, True)])
    def test_start_checks_model_and_drives_mqtt(self, engine, monkeypatch, health, present):
        monkeypatch.setattr(engine.ollama, "health", lambda: health)
        monkeypatch.setattr(engine.ollama, "model_present", lambda: present)
        engine.start()
        assert any(call[0] == "will_set" for call in engine.mqtt.calls)
        assert ("connect", engine.cfg.mqtt_host, engine.cfg.mqtt_port, 60) in engine.mqtt.calls
        assert ("loop_forever",) in engine.mqtt.calls

    def test_stop_publishes_offline_and_disconnects(self, engine):
        engine.stop()
        assert json.loads(engine.mqtt.published[-1][1])["state"] == "offline"
        assert ("disconnect",) in engine.mqtt.calls


class TestTimeAndFeverDefenses:
    def test_naive_vital_timestamp_is_treated_as_utc(self):
        naive_utc = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        reading = VitalReading(72, "bpm", naive_utc)
        assert abs(reading.age_seconds()) < 5

    def test_fever_burden_skips_non_numeric_and_bad_timestamps(self):
        cache = VitalsCache()
        now = datetime.now(timezone.utc)
        cache.update("p", "temperature_c", VitalReading("hot", "C", now.isoformat()))
        cache.update("p", "temperature_c", VitalReading(39.0, "C", "bad-time"))
        cache.update(
            "p", "temperature_c",
            VitalReading(39.0, "C", (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat()),
        )
        cache.update("p", "temperature_c", VitalReading(39.0, "C", now.isoformat()))
        assert cache.fever_burden_minutes("p") == pytest.approx(5.0, abs=0.2)


class CliMqtt(LifecycleMqtt):
    pass


class CliEngine:
    instances = []
    generate_error = None
    interrupt_start = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.mqtt = CliMqtt()
        self.retriever = SimpleNamespace(
            retrieve=lambda query, n_results: [{"text": "guide", "source": "WHO"}]
        )
        self.prompts = SimpleNamespace(
            build=lambda **kwargs: f"PROMPT:{kwargs['patient_id']}:{kwargs['user_query']}"
        )

        def generate(prompt):
            if self.generate_error:
                raise self.generate_error
            return "answer"

        self.ollama = SimpleNamespace(generate=generate)
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True
        if self.interrupt_start:
            raise KeyboardInterrupt

    def stop(self):
        self.stopped = True


class TestCommandLine:
    @pytest.fixture(autouse=True)
    def _reset(self):
        CliEngine.instances.clear()
        CliEngine.generate_error = None
        CliEngine.interrupt_start = False

    def test_one_shot_mode_builds_answer_and_cleans_up(self, monkeypatch, capsys):
        monkeypatch.setattr(ai_mod, "MedicalAIEngine", CliEngine)
        monkeypatch.setattr(ai_mod.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["medical-ai", "--ask", "fever?", "--patient", "patient-a"],
        )
        ai_mod.main()
        engine = CliEngine.instances[-1]
        assert "answer" in capsys.readouterr().out
        assert ("loop_start",) in engine.mqtt.calls
        assert ("loop_stop",) in engine.mqtt.calls
        assert ("disconnect",) in engine.mqtt.calls

    def test_one_shot_failure_still_cleans_up(self, monkeypatch):
        CliEngine.generate_error = requests.Timeout("model timeout")
        monkeypatch.setattr(ai_mod, "MedicalAIEngine", CliEngine)
        monkeypatch.setattr(ai_mod.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(sys, "argv", ["medical-ai", "--ask", "fever?"])
        with pytest.raises(requests.Timeout):
            ai_mod.main()
        engine = CliEngine.instances[-1]
        assert ("loop_stop",) in engine.mqtt.calls
        assert ("disconnect",) in engine.mqtt.calls

    def test_daemon_keyboard_interrupt_calls_stop(self, monkeypatch):
        CliEngine.interrupt_start = True
        monkeypatch.setattr(ai_mod, "MedicalAIEngine", CliEngine)
        monkeypatch.setattr(sys, "argv", ["medical-ai"])
        ai_mod.main()
        engine = CliEngine.instances[-1]
        assert engine.started is True
        assert engine.stopped is True
