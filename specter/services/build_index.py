#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         SPECTER LIBRARY — RAG INDEX BUILDER  (build_index.py)              ║
║                                                                              ║
║  Scans all PDFs in the library, chunks them, generates local embeddings     ║
║  via Ollama nomic-embed-text, and stores them in ChromaDB.                  ║
║                                                                              ║
║  Run manually:                                                               ║
║    python3 build_index.py --pdf-dir /mnt/specter/library/pdf                ║
║             --index-dir /mnt/specter/library/vector_index                   ║
║                                                                              ║
║  Runs automatically nightly via specter-index-builder.timer                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [specter-index] %(message)s",
)
log = logging.getLogger("specter.build_index")

OLLAMA_HOST  = "http://localhost:11434"
EMBED_MODEL  = "nomic-embed-text"
CHUNK_SIZE   = 512      # characters per chunk
CHUNK_OVERLAP = 64      # overlap between chunks
BATCH_SIZE   = 32       # embeddings per Ollama call


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start  = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract plain text from a PDF file."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)
    except ImportError:
        pass

    try:
        import PyPDF2
        text_parts = []
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        log.warning("PDF extract failed for %s: %s", pdf_path.name, e)
        return ""


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Generate embeddings via local Ollama nomic-embed-text."""
    embeddings = []
    for text in texts:
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=30,
            )
            if resp.ok:
                embeddings.append(resp.json().get("embedding", []))
            else:
                embeddings.append([])
        except Exception as e:
            log.debug("Embedding error: %s", e)
            embeddings.append([])
    return embeddings


def file_hash(path: Path) -> str:
    """MD5 hash of file for change detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_index(pdf_dir: Path, index_dir: Path) -> None:
    import chromadb

    index_dir.mkdir(parents=True, exist_ok=True)
    client     = chromadb.PersistentClient(path=str(index_dir))
    collection = client.get_or_create_collection(
        name="specter_library",
        metadata={"hnsw:space": "cosine"},
    )

    # Track indexed files to skip unchanged ones
    state_path = index_dir / "index_state.json"
    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            state = {}

    pdf_files = list(pdf_dir.rglob("*.pdf"))
    log.info("Found %d PDFs to index", len(pdf_files))

    total_chunks  = 0
    indexed_files = 0
    skipped_files = 0

    for pdf_path in pdf_files:
        relative   = str(pdf_path.relative_to(pdf_dir))
        file_md5   = file_hash(pdf_path)
        category   = pdf_path.parent.name

        if state.get(relative) == file_md5:
            log.debug("Unchanged, skipping: %s", pdf_path.name)
            skipped_files += 1
            continue

        log.info("Indexing: %s", relative)
        text = extract_pdf_text(pdf_path)

        if not text.strip():
            log.warning("No text extracted from %s — skipping", pdf_path.name)
            continue

        chunks = chunk_text(text)
        log.info("  %d chunks from %s", len(chunks), pdf_path.name)

        # Generate IDs
        ids = [
            f"{file_md5}_{i}"
            for i in range(len(chunks))
        ]

        # Metadata per chunk
        metadatas = [
            {
                "source":   str(pdf_path),
                "filename": pdf_path.name,
                "category": category,
                "page":     i,
                "file_hash": file_md5,
            }
            for i in range(len(chunks))
        ]

        # Generate embeddings in batches
        all_embeddings = []
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            embs  = get_embeddings(batch)
            all_embeddings.extend(embs)
            log.debug("  Embedded batch %d/%d", i // BATCH_SIZE + 1,
                      (len(chunks) + BATCH_SIZE - 1) // BATCH_SIZE)

        # Filter out empty embeddings
        valid = [
            (chunk, emb, meta, id_)
            for chunk, emb, meta, id_ in zip(chunks, all_embeddings, metadatas, ids)
            if emb
        ]

        if not valid:
            log.warning("No valid embeddings for %s", pdf_path.name)
            continue

        v_chunks, v_embs, v_metas, v_ids = zip(*valid)

        # Delete existing entries for this file (if re-indexing)
        try:
            existing = collection.get(
                where={"file_hash": file_md5},
                include=[],
            )
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
        except Exception:
            pass

        # Add to ChromaDB
        try:
            collection.add(
                ids=list(v_ids),
                documents=list(v_chunks),
                embeddings=list(v_embs),
                metadatas=list(v_metas),
            )
            total_chunks  += len(v_ids)
            indexed_files += 1
            state[relative] = file_md5
            log.info("  ✓ Indexed %d chunks", len(v_ids))
        except Exception as e:
            log.error("ChromaDB add failed for %s: %s", pdf_path.name, e)

    # Save state
    state_path.write_text(json.dumps(state, indent=2))

    log.info(
        "Index complete: %d files indexed, %d skipped, %d total chunks in store",
        indexed_files, skipped_files, collection.count()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="SPECTER Library RAG Index Builder")
    parser.add_argument("--pdf-dir",   default="/mnt/specter/library/pdf",
                        help="Root directory containing categorized PDF files")
    parser.add_argument("--index-dir", default="/mnt/specter/library/vector_index",
                        help="ChromaDB persistent storage directory")
    args = parser.parse_args()

    pdf_dir   = Path(args.pdf_dir)
    index_dir = Path(args.index_dir)

    if not pdf_dir.exists():
        log.error("PDF directory not found: %s", pdf_dir)
        return 1

    log.info("SPECTER RAG Index Builder starting")
    log.info("PDF dir:   %s", pdf_dir)
    log.info("Index dir: %s", index_dir)
    log.info("Model:     %s (via Ollama)", EMBED_MODEL)

    t0 = time.time()
    build_index(pdf_dir, index_dir)
    log.info("Done in %.1f minutes", (time.time() - t0) / 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
