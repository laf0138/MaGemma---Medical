#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          SPECTER LIBRARY RAG API  —  library_api.py                         ║
║                                                                              ║
║  Flask REST API bridging the operator to the offline AI library.            ║
║  Runs on Jetson Orin Nano Super at 192.168.1.5:5001                        ║
║                                                                              ║
║  Flow:                                                                       ║
║    Query → Kiwix full-text search → PDF vector search → Build context       ║
║          → Ollama LLaMA 3.2 3B → Grounded answer + sources                 ║
║                                                                              ║
║  Endpoints:                                                                  ║
║    POST /ask           — RAG query (JSON: {"query": "..."})                 ║
║    GET  /search        — Kiwix keyword search (?q=...&limit=5)             ║
║    GET  /status        — Service health + model status                      ║
║    GET  /categories    — List available library categories                  ║
║    POST /mqtt/bridge   — Internal: MQTT → API bridge                        ║
║                                                                              ║
║  MQTT:                                                                       ║
║    Subscribe: shtf/library/ask       — trigger a query                      ║
║    Publish:   shtf/library/response  — answer                               ║
║    Publish:   shtf/library/status    — heartbeat                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, request

# ─── Config ───────────────────────────────────────────────────────────────────
VERSION         = "1.0.0"
CONFIG_PATH     = Path("/etc/specter/library.json")
OLLAMA_HOST     = os.environ.get("OLLAMA_HOST",        "http://localhost:11434")
KIWIX_URL       = os.environ.get("KIWIX_URL",          "http://localhost:8080")
PDF_DIR         = Path(os.environ.get("PDF_DIR",        "/mnt/specter/library/pdf"))
VECTOR_INDEX    = Path(os.environ.get("VECTOR_INDEX_DIR", "/mnt/specter/library/vector_index"))
MQTT_BROKER     = os.environ.get("MQTT_BROKER",        "192.168.1.1")
API_PORT        = int(os.environ.get("LIBRARY_API_PORT", "5001"))

TOPIC_ASK       = "shtf/library/ask"
TOPIC_RESPONSE  = "shtf/library/response"
TOPIC_STATUS    = "shtf/library/status"

DEFAULT_MODEL   = "llama3.2:3b-instruct-q4_K_M"
EMBED_MODEL     = "nomic-embed-text"
MAX_TOKENS      = 512
TEMPERATURE     = 0.2
CONTEXT_DOCS    = 5    # number of source chunks to feed LLM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-library] %(message)s",
)
log = logging.getLogger("specter.library_api")

app = Flask(__name__)
_start_time = time.time()

# ─── Load config ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}

cfg = load_config()

# ─── Kiwix search ─────────────────────────────────────────────────────────────

def kiwix_search(query: str, limit: int = 5) -> list[dict]:
    """Full-text search across all loaded ZIM files via kiwix-serve."""
    results = []
    try:
        resp = requests.get(
            f"{KIWIX_URL}/search",
            params={"content": "", "pattern": query, "books.count": limit},
            timeout=10,
        )
        if resp.ok:
            # Parse kiwix-serve search results (HTML response)
            # Extract article titles and snippets
            import re
            html = resp.text
            # Find search result entries
            titles   = re.findall(r'<div class="title"[^>]*>(.*?)</div>', html, re.S)
            snippets = re.findall(r'<div class="snippet"[^>]*>(.*?)</div>', html, re.S)
            links    = re.findall(r'href="(/[^"]+)"', html)
            for i, title in enumerate(titles[:limit]):
                clean_title   = re.sub(r'<[^>]+>', '', title).strip()
                clean_snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
                link = links[i] if i < len(links) else ""
                results.append({
                    "title":   clean_title,
                    "snippet": clean_snippet,
                    "url":     f"{KIWIX_URL}{link}",
                    "source":  "kiwix",
                })
    except Exception as e:
        log.warning("Kiwix search error: %s", e)
    return results

# ─── PDF vector search (ChromaDB) ─────────────────────────────────────────────

_chroma_client  = None
_chroma_collection = None

def get_chroma():
    global _chroma_client, _chroma_collection
    if _chroma_client is None:
        try:
            import chromadb
            _chroma_client = chromadb.PersistentClient(path=str(VECTOR_INDEX))
            _chroma_collection = _chroma_client.get_or_create_collection(
                name="specter_library",
                metadata={"hnsw:space": "cosine"},
            )
            log.info("ChromaDB loaded: %d documents", _chroma_collection.count())
        except Exception as e:
            log.warning("ChromaDB unavailable: %s", e)
    return _chroma_collection

def vector_search(query: str, top_k: int = CONTEXT_DOCS) -> list[dict]:
    """Semantic vector search over indexed PDFs."""
    col = get_chroma()
    if col is None:
        return []
    try:
        results = col.query(
            query_texts=[query],
            n_results=min(top_k, col.count() or 1),
            include=["documents", "metadatas", "distances"],
        )
        docs = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            docs.append({
                "content":  doc,
                "source":   meta.get("source", "unknown"),
                "category": meta.get("category", ""),
                "page":     meta.get("page", 0),
                "score":    round(1.0 - dist, 4),
            })
        return docs
    except Exception as e:
        log.warning("Vector search error: %s", e)
        return []

# ─── Ollama inference ─────────────────────────────────────────────────────────

def ollama_generate(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Call local Ollama LLM and return generated text."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model":  model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": TEMPERATURE,
                    "num_predict": MAX_TOKENS,
                    "stop": ["<|end|>", "###"],
                },
            },
            timeout=120,
        )
        if resp.ok:
            return resp.json().get("response", "").strip()
        else:
            log.error("Ollama error %d: %s", resp.status_code, resp.text[:200])
            return f"[Ollama error {resp.status_code}]"
    except requests.exceptions.ConnectionError:
        return "[Ollama not reachable — is specter-ollama.service running?]"
    except Exception as e:
        log.error("Ollama exception: %s", e)
        return f"[Ollama exception: {e}]"

def ollama_status() -> dict:
    """Check Ollama health and list loaded models."""
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if resp.ok:
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"ok": True, "models": models}
    except Exception:
        pass
    return {"ok": False, "models": []}

# ─── RAG pipeline ─────────────────────────────────────────────────────────────

RAG_SYSTEM_PROMPT = """You are SPECTER-AI, an offline emergency reference assistant running on an air-gapped tactical communications system.

You have access to a curated library including:
- Medical and trauma references (MSF Clinical Guidelines, WHO, US Army SF Medical Handbook)
- CBRN and nuclear survival (ORNL Nuclear War Survival Skills, FEMA)
- Food preservation and agriculture (USDA, FAO)
- Construction and shelter (Army field manuals, UNHCR)
- Wikipedia, Wikibooks, Khan Academy
- LDS Standard Works and regional references

Rules:
1. Answer directly and practically. This is a field system — be concise and actionable.
2. Cite your sources by name (e.g. "Per MSF Clinical Guidelines..." or "Per FM 5-34...").
3. For medical questions, state that this is reference information only and professional judgment is required.
4. If the context does not contain the answer, say so clearly — do not hallucinate.
5. Prioritize Tier 1/Trust A sources. Flag if only lower-trust sources are available.
"""

def build_rag_prompt(query: str, kiwix_results: list, vector_results: list) -> str:
    context_parts = []

    if vector_results:
        context_parts.append("=== PDF LIBRARY EXCERPTS ===")
        for i, doc in enumerate(vector_results[:3], 1):
            src  = Path(doc["source"]).name if doc["source"] else "unknown"
            page = doc.get("page", "")
            context_parts.append(
                f"[{i}] Source: {src}"
                + (f" p.{page}" if page else "")
                + f"\n{doc['content'][:600]}"
            )

    if kiwix_results:
        context_parts.append("\n=== WIKI/ENCYCLOPEDIA RESULTS ===")
        for i, result in enumerate(kiwix_results[:3], 1):
            context_parts.append(
                f"[{i}] {result['title']}\n{result['snippet'][:400]}"
            )

    context = "\n\n".join(context_parts) if context_parts else "No library context retrieved."

    return f"""{RAG_SYSTEM_PROMPT}

=== REFERENCE CONTEXT ===
{context}

=== OPERATOR QUERY ===
{query}

=== YOUR RESPONSE ==="""

def rag_query(query: str, model: str = DEFAULT_MODEL) -> dict:
    """Full RAG pipeline: search → context → LLM → response."""
    t0 = time.time()

    kiwix_results  = kiwix_search(query, limit=CONTEXT_DOCS)
    vector_results = vector_search(query, top_k=CONTEXT_DOCS)

    prompt   = build_rag_prompt(query, kiwix_results, vector_results)
    answer   = ollama_generate(prompt, model=model)
    elapsed  = round(time.time() - t0, 2)

    sources = []
    for r in vector_results[:3]:
        sources.append({
            "type":     "pdf",
            "file":     Path(r["source"]).name,
            "category": r.get("category", ""),
            "score":    r.get("score", 0),
        })
    for r in kiwix_results[:3]:
        sources.append({
            "type":  "wiki",
            "title": r["title"],
            "url":   r["url"],
        })

    return {
        "query":       query,
        "answer":      answer,
        "sources":     sources,
        "model":       model,
        "elapsed_sec": elapsed,
        "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/ask", methods=["POST"])
def api_ask():
    data  = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    model = data.get("model", DEFAULT_MODEL)

    if not query:
        return jsonify({"error": "Missing 'query' field"}), 400

    log.info("RAG query: %s", query[:80])
    result = rag_query(query, model=model)
    log.info("Answer in %.1fs, %d sources", result["elapsed_sec"], len(result["sources"]))

    return jsonify(result)


@app.route("/search", methods=["GET"])
def api_search():
    q     = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 5))
    if not q:
        return jsonify({"error": "Missing 'q' parameter"}), 400

    kiwix_results  = kiwix_search(q, limit=limit)
    vector_results = vector_search(q, top_k=limit)

    return jsonify({
        "query":         q,
        "kiwix_results": kiwix_results,
        "pdf_results":   vector_results,
        "total":         len(kiwix_results) + len(vector_results),
    })


@app.route("/status", methods=["GET"])
def api_status():
    ollama = ollama_status()
    col    = get_chroma()
    doc_count = col.count() if col else 0

    # Count ZIM and PDF files
    zim_count = len(list(Path(cfg.get("kiwix", {}).get("zim_dir",
        "/mnt/specter/library/zim")).rglob("*.zim")))
    pdf_count = len(list(Path(cfg.get("rag", {}).get("pdf_dir",
        "/mnt/specter/library/pdf")).rglob("*.pdf")))

    return jsonify({
        "service":       "specter-library-api",
        "version":       VERSION,
        "uptime_sec":    int(time.time() - _start_time),
        "ollama":        ollama,
        "kiwix_url":     KIWIX_URL,
        "zim_files":     zim_count,
        "pdf_files":     pdf_count,
        "indexed_chunks": doc_count,
        "timestamp":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


@app.route("/categories", methods=["GET"])
def api_categories():
    categories = cfg.get("categories", {})
    # Add file counts per category
    result = {}
    for cat, desc in categories.items():
        zim_path = Path(cfg.get("kiwix", {}).get("zim_dir",
            "/mnt/specter/library/zim")) / cat
        pdf_path = Path(cfg.get("rag", {}).get("pdf_dir",
            "/mnt/specter/library/pdf")) / cat
        result[cat] = {
            "description": desc,
            "zim_count":   len(list(zim_path.glob("*.zim"))) if zim_path.exists() else 0,
            "pdf_count":   len(list(pdf_path.glob("*.pdf"))) if pdf_path.exists() else 0,
        }
    return jsonify(result)


# ─── MQTT bridge ──────────────────────────────────────────────────────────────

class LibraryMQTT:
    def __init__(self, broker: str):
        self.broker = broker
        self._client = None
        self._stop   = threading.Event()
        self._connect()

    def _connect(self):
        try:
            import paho.mqtt.client as mqtt
            client = mqtt.Client(client_id="specter_library_api")
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.connect(self.broker, 1883, 60)
            client.loop_start()
            self._client = client
            log.info("Library MQTT connected to %s", self.broker)
        except Exception as e:
            log.warning("Library MQTT unavailable: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(TOPIC_ASK)
            log.info("Subscribed to %s", TOPIC_ASK)

    def _on_message(self, client, userdata, msg):
        """Handle MQTT query: parse JSON, run RAG, publish response."""
        try:
            payload = json.loads(msg.payload.decode())
            query   = payload.get("query", "").strip()
            if not query:
                return
            log.info("MQTT query: %s", query[:60])
            result = rag_query(query)
            self.publish(TOPIC_RESPONSE, result)
        except Exception as e:
            log.error("MQTT message handler error: %s", e)

    def publish(self, topic: str, data) -> None:
        if not self._client:
            return
        try:
            payload = json.dumps(data) if not isinstance(data, str) else data
            self._client.publish(topic, payload)
        except Exception as e:
            log.debug("MQTT publish error: %s", e)

    def publish_status(self) -> None:
        """Periodic heartbeat to MQTT."""
        while not self._stop.is_set():
            try:
                ollama = ollama_status()
                self.publish(TOPIC_STATUS, {
                    "service":   "specter-library-api",
                    "version":   VERSION,
                    "uptime":    int(time.time() - _start_time),
                    "ollama_ok": ollama["ok"],
                    "models":    ollama["models"],
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            except Exception:
                pass
            self._stop.wait(30)

    def start_heartbeat(self):
        t = threading.Thread(target=self.publish_status, daemon=True, name="library-heartbeat")
        t.start()

    def stop(self):
        self._stop.set()
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    mqtt = LibraryMQTT(MQTT_BROKER)
    mqtt.start_heartbeat()

    def _stop(sig, frame):
        log.info("Library API stopping")
        mqtt.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("SPECTER Library RAG API starting on 0.0.0.0:%d", API_PORT)
    log.info("Ollama: %s | Kiwix: %s", OLLAMA_HOST, KIWIX_URL)

    app.run(host="0.0.0.0", port=API_PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
