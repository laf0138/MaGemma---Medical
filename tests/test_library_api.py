"""
Tests for services/library_api.py - the RAG (retrieval-augmented
generation) pipeline behind the /ask endpoint: Kiwix full-text search,
ChromaDB vector search, prompt assembly, and the Ollama call, plus the
Flask routes and MQTT bridge that expose them.

External services (Ollama, Kiwix, ChromaDB, MQTT) are mocked throughout -
this suite checks SPECTER's own parsing/assembly/error-handling logic.
"""
import json

import pytest
import requests

import services.library_api as lib


class FakeResponse:
    def __init__(self, ok=True, status_code=200, json_data=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


# ---------------------------------------------------------------------------
# kiwix_search - HTML scraping
# ---------------------------------------------------------------------------

KIWIX_HTML = """
<div class="title">Hyperthermia</div>
<div class="snippet">Body temperature regulation and heat illness.</div>
<a href="/viewer#msf/A/Hyperthermia">link</a>
<div class="title">Hypothermia</div>
<div class="snippet">Cold exposure and rewarming.</div>
<a href="/viewer#msf/A/Hypothermia">link</a>
"""


class TestKiwixSearch:
    def test_parses_titles_snippets_and_links(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "get", lambda *a, **k: FakeResponse(text=KIWIX_HTML))
        results = lib.kiwix_search("hypo", limit=5)
        assert len(results) == 2
        assert results[0]["title"] == "Hyperthermia"
        assert "heat illness" in results[0]["snippet"]
        assert results[0]["url"].startswith(lib.KIWIX_URL)
        assert results[0]["source"] == "kiwix"

    def test_respects_limit(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "get", lambda *a, **k: FakeResponse(text=KIWIX_HTML))
        results = lib.kiwix_search("hypo", limit=1)
        assert len(results) == 1

    def test_non_ok_response_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "get", lambda *a, **k: FakeResponse(ok=False))
        assert lib.kiwix_search("q") == []

    def test_request_exception_returns_empty_list_not_raise(self, monkeypatch):
        def raise_conn_error(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(lib.requests, "get", raise_conn_error)
        assert lib.kiwix_search("q") == []


# ---------------------------------------------------------------------------
# ollama_generate / ollama_status
# ---------------------------------------------------------------------------

class TestOllamaGenerate:
    def test_success_returns_stripped_response_text(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "post",
                             lambda *a, **k: FakeResponse(json_data={"response": "  answer text  "}))
        assert lib.ollama_generate("prompt") == "answer text"

    def test_non_ok_status_returns_error_marker(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "post",
                             lambda *a, **k: FakeResponse(ok=False, status_code=500, text="boom"))
        result = lib.ollama_generate("prompt")
        assert result == "[Ollama error 500]"

    def test_connection_error_returns_not_reachable_message(self, monkeypatch):
        def raise_conn(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(lib.requests, "post", raise_conn)
        assert "not reachable" in lib.ollama_generate("prompt")

    def test_generic_exception_is_captured_in_message(self, monkeypatch):
        def raise_boom(*a, **k):
            raise ValueError("weird failure")
        monkeypatch.setattr(lib.requests, "post", raise_boom)
        result = lib.ollama_generate("prompt")
        assert "weird failure" in result


class TestOllamaStatus:
    def test_success_lists_model_names(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "get", lambda *a, **k: FakeResponse(
            json_data={"models": [{"name": "llama3.2:3b"}, {"name": "medgemma:4b"}]}))
        status = lib.ollama_status()
        assert status == {"ok": True, "models": ["llama3.2:3b", "medgemma:4b"]}

    def test_non_ok_response_is_not_ok(self, monkeypatch):
        monkeypatch.setattr(lib.requests, "get", lambda *a, **k: FakeResponse(ok=False))
        assert lib.ollama_status() == {"ok": False, "models": []}

    def test_exception_is_not_ok(self, monkeypatch):
        def raise_conn(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(lib.requests, "get", raise_conn)
        assert lib.ollama_status() == {"ok": False, "models": []}


# ---------------------------------------------------------------------------
# build_rag_prompt - pure context assembly
# ---------------------------------------------------------------------------

class TestBuildRagPrompt:
    def test_no_results_says_so_explicitly(self):
        prompt = lib.build_rag_prompt("query", [], [])
        assert "No library context retrieved." in prompt
        assert "query" in prompt

    def test_vector_results_include_source_filename_not_full_path(self):
        prompt = lib.build_rag_prompt("q", [], [
            {"source": "/mnt/specter/library/pdf/msf_guidelines.pdf", "page": 12, "content": "text"}
        ])
        assert "msf_guidelines.pdf" in prompt
        assert "/mnt/specter/library/pdf/" not in prompt
        assert "p.12" in prompt

    def test_vector_result_without_page_omits_page_suffix(self):
        prompt = lib.build_rag_prompt("q", [], [{"source": "doc.pdf", "content": "text"}])
        assert "p." not in prompt.split("=== REFERENCE CONTEXT ===")[1].split("=== OPERATOR")[0]

    def test_only_first_three_vector_results_used(self):
        vector_results = [
            {"source": f"doc{i}.pdf", "content": f"content{i}"} for i in range(5)
        ]
        prompt = lib.build_rag_prompt("q", [], vector_results)
        assert "doc0.pdf" in prompt and "doc2.pdf" in prompt
        assert "doc3.pdf" not in prompt and "doc4.pdf" not in prompt

    def test_vector_content_truncated_to_600_chars(self):
        long_content = "x" * 1000
        prompt = lib.build_rag_prompt("q", [], [{"source": "doc.pdf", "content": long_content}])
        assert "x" * 601 not in prompt

    def test_only_first_three_kiwix_results_used(self):
        kiwix_results = [
            {"title": f"Title{i}", "snippet": f"snippet{i}"} for i in range(5)
        ]
        prompt = lib.build_rag_prompt("q", kiwix_results, [])
        assert "Title0" in prompt and "Title2" in prompt
        assert "Title3" not in prompt and "Title4" not in prompt

    def test_kiwix_snippet_truncated_to_400_chars(self):
        long_snippet = "y" * 1000
        prompt = lib.build_rag_prompt("q", [{"title": "T", "snippet": long_snippet}], [])
        assert "y" * 401 not in prompt

    def test_pdf_section_precedes_wiki_section_when_both_present(self):
        prompt = lib.build_rag_prompt(
            "q",
            [{"title": "T", "snippet": "s"}],
            [{"source": "doc.pdf", "content": "c"}],
        )
        assert prompt.index("PDF LIBRARY EXCERPTS") < prompt.index("WIKI/ENCYCLOPEDIA RESULTS")

    def test_operator_query_appears_verbatim_at_end(self):
        prompt = lib.build_rag_prompt("what do I do about a burn", [], [])
        assert prompt.rstrip().endswith(
            "=== OPERATOR QUERY ===\nwhat do I do about a burn\n\n=== YOUR RESPONSE ==="
        )


# ---------------------------------------------------------------------------
# vector_search - scoring/assembly (ChromaDB itself mocked out)
# ---------------------------------------------------------------------------

class FakeCollection:
    def __init__(self, count_val, documents, metadatas, distances):
        self._count = count_val
        self._documents = documents
        self._metadatas = metadatas
        self._distances = distances

    def count(self):
        return self._count

    def query(self, query_texts, n_results, include):
        return {
            "documents": [self._documents],
            "metadatas": [self._metadatas],
            "distances": [self._distances],
        }


class TestVectorSearch:
    def test_no_chroma_collection_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(lib, "get_chroma", lambda: None)
        assert lib.vector_search("q") == []

    def test_assembles_docs_with_rounded_similarity_score(self, monkeypatch):
        col = FakeCollection(
            count_val=2,
            documents=["excerpt one"],
            metadatas=[{"source": "doc.pdf", "category": "medical", "page": 3}],
            distances=[0.12345],
        )
        monkeypatch.setattr(lib, "get_chroma", lambda: col)
        results = lib.vector_search("q", top_k=5)
        assert results == [{
            "content": "excerpt one",
            "source": "doc.pdf",
            "category": "medical",
            "page": 3,
            "score": round(1.0 - 0.12345, 4),
        }]

    def test_query_exception_returns_empty_list(self, monkeypatch):
        class BrokenCollection:
            def count(self):
                return 1
            def query(self, *a, **k):
                raise RuntimeError("index corrupted")
        monkeypatch.setattr(lib, "get_chroma", lambda: BrokenCollection())
        assert lib.vector_search("q") == []


# ---------------------------------------------------------------------------
# rag_query - full pipeline assembly
# ---------------------------------------------------------------------------

class TestRagQuery:
    def test_assembles_pdf_and_wiki_sources_capped_at_three_each(self, monkeypatch):
        monkeypatch.setattr(lib, "kiwix_search", lambda q, limit=5: [
            {"title": f"T{i}", "snippet": "s", "url": f"http://x/{i}"} for i in range(5)
        ])
        monkeypatch.setattr(lib, "vector_search", lambda q, top_k=5: [
            {"source": f"doc{i}.pdf", "category": "medical", "score": 0.9, "content": "c"} for i in range(5)
        ])
        monkeypatch.setattr(lib, "ollama_generate", lambda prompt, model=lib.DEFAULT_MODEL: "the answer")

        result = lib.rag_query("burn treatment")

        pdf_sources = [s for s in result["sources"] if s["type"] == "pdf"]
        wiki_sources = [s for s in result["sources"] if s["type"] == "wiki"]
        assert len(pdf_sources) == 3
        assert len(wiki_sources) == 3
        assert result["answer"] == "the answer"
        assert result["query"] == "burn treatment"
        assert result["elapsed_sec"] >= 0

    def test_empty_results_still_returns_well_formed_response(self, monkeypatch):
        monkeypatch.setattr(lib, "kiwix_search", lambda q, limit=5: [])
        monkeypatch.setattr(lib, "vector_search", lambda q, top_k=5: [])
        monkeypatch.setattr(lib, "ollama_generate", lambda prompt, model=lib.DEFAULT_MODEL: "no context available")

        result = lib.rag_query("obscure question")
        assert result["sources"] == []
        assert result["answer"] == "no context available"


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    lib.app.config["TESTING"] = True
    return lib.app.test_client()


class TestAskRoute:
    def test_missing_query_returns_400(self, client):
        resp = client.post("/ask", json={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_valid_query_returns_rag_result(self, client, monkeypatch):
        monkeypatch.setattr(lib, "rag_query", lambda query, model=lib.DEFAULT_MODEL: {
            "query": query, "answer": "ans", "sources": [], "model": model,
            "elapsed_sec": 0.1, "timestamp": "2026-01-01T00:00:00Z",
        })
        resp = client.post("/ask", json={"query": "what about frostbite"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["answer"] == "ans"
        assert data["query"] == "what about frostbite"

    def test_whitespace_only_query_returns_400(self, client):
        resp = client.post("/ask", json={"query": "   "})
        assert resp.status_code == 400


class TestSearchRoute:
    def test_missing_q_returns_400(self, client):
        resp = client.get("/search")
        assert resp.status_code == 400

    def test_combines_kiwix_and_pdf_results(self, client, monkeypatch):
        monkeypatch.setattr(lib, "kiwix_search", lambda q, limit=5: [{"title": "A"}])
        monkeypatch.setattr(lib, "vector_search", lambda q, top_k=5: [{"source": "b.pdf"}, {"source": "c.pdf"}])
        resp = client.get("/search?q=burns")
        data = resp.get_json()
        assert data["total"] == 3
        assert data["query"] == "burns"


class TestStatusRoute:
    def test_reports_ollama_and_index_state(self, client, monkeypatch):
        monkeypatch.setattr(lib, "ollama_status", lambda: {"ok": True, "models": ["m1"]})
        monkeypatch.setattr(lib, "get_chroma", lambda: FakeCollection(7, [], [], []))
        resp = client.get("/status")
        data = resp.get_json()
        assert data["ollama"] == {"ok": True, "models": ["m1"]}
        assert data["indexed_chunks"] == 7
        assert data["service"] == "specter-library-api"

    def test_no_chroma_reports_zero_indexed_chunks(self, client, monkeypatch):
        monkeypatch.setattr(lib, "ollama_status", lambda: {"ok": False, "models": []})
        monkeypatch.setattr(lib, "get_chroma", lambda: None)
        resp = client.get("/status")
        assert resp.get_json()["indexed_chunks"] == 0


class TestCategoriesRoute:
    def test_reports_zero_counts_for_missing_directories(self, client, monkeypatch):
        monkeypatch.setattr(lib, "cfg", {"categories": {"medical": "Medical references"}})
        resp = client.get("/categories")
        data = resp.get_json()
        assert data["medical"]["description"] == "Medical references"
        assert data["medical"]["zim_count"] == 0
        assert data["medical"]["pdf_count"] == 0

    def test_no_categories_configured_returns_empty_object(self, client, monkeypatch):
        monkeypatch.setattr(lib, "cfg", {})
        resp = client.get("/categories")
        assert resp.get_json() == {}


# ---------------------------------------------------------------------------
# LibraryMQTT - dispatch and publish (real network connect stubbed out)
# ---------------------------------------------------------------------------

class FakeMQTTClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))


@pytest.fixture
def library_mqtt(monkeypatch):
    monkeypatch.setattr(lib.LibraryMQTT, "_connect", lambda self: None)
    bridge = lib.LibraryMQTT("broker-host")
    bridge._client = FakeMQTTClient()
    return bridge


def msg(payload: dict):
    class Msg:
        pass
    m = Msg()
    m.payload = json.dumps(payload).encode()
    return m


class TestLibraryMQTTDispatch:
    def test_valid_query_runs_rag_and_publishes_response(self, library_mqtt, monkeypatch):
        monkeypatch.setattr(lib, "rag_query", lambda q: {"answer": "ans", "query": q})
        library_mqtt._on_message(None, None, msg({"query": "what now"}))
        assert len(library_mqtt._client.published) == 1
        topic, payload = library_mqtt._client.published[0]
        assert topic == lib.TOPIC_RESPONSE
        assert json.loads(payload)["answer"] == "ans"

    def test_empty_query_does_not_publish(self, library_mqtt, monkeypatch):
        called = []
        monkeypatch.setattr(lib, "rag_query", lambda q: called.append(1))
        library_mqtt._on_message(None, None, msg({"query": "   "}))
        assert called == []
        assert library_mqtt._client.published == []

    def test_non_json_payload_does_not_raise(self, library_mqtt):
        class BadMsg:
            payload = b"not json"
        library_mqtt._on_message(None, None, BadMsg())
        assert library_mqtt._client.published == []

    def test_publish_with_no_client_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(lib.LibraryMQTT, "_connect", lambda self: None)
        bridge = lib.LibraryMQTT("broker-host")
        bridge.publish("some/topic", {"a": 1})  # _client is None - must not raise
