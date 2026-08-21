"""
Tests for services/build_index.py's pure/lightly-mocked pieces: text
chunking, file hashing, and embedding generation. build_index() itself
requires a real ChromaDB instance and isn't covered here - it's a thin
orchestration layer over these already-tested pieces plus ChromaDB calls.
"""
import hashlib
from pathlib import Path

import pytest

import services.build_index as bi


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_text_shorter_than_chunk_size_is_a_single_chunk(self):
        chunks = bi.chunk_text("hello", chunk_size=10, overlap=2)
        assert chunks == ["hello"]

    def test_empty_text_produces_no_chunks(self):
        assert bi.chunk_text("", chunk_size=10, overlap=2) == []

    def test_consecutive_chunks_overlap_by_requested_amount(self):
        text = "0123456789ABCDEFGHIJ"  # 20 chars
        chunks = bi.chunk_text(text, chunk_size=10, overlap=3)
        assert len(chunks) == 3
        # last 3 chars of chunk[i] == first 3 chars of chunk[i+1]
        assert chunks[0][-3:] == chunks[1][:3]
        assert chunks[1][-3:] == chunks[2][:3]

    def test_zero_overlap_produces_contiguous_non_overlapping_chunks(self):
        text = "0123456789ABCDEFGHIJ"  # 20 chars
        chunks = bi.chunk_text(text, chunk_size=5, overlap=0)
        assert chunks == ["01234", "56789", "ABCDE", "FGHIJ"]
        assert "".join(chunks) == text

    def test_default_constants_chunk_a_long_document(self):
        text = "word " * 1000  # 5000 chars, well over default CHUNK_SIZE
        chunks = bi.chunk_text(text)
        assert len(chunks) > 1
        assert all(len(c) <= bi.CHUNK_SIZE for c in chunks)


# ---------------------------------------------------------------------------
# file_hash
# ---------------------------------------------------------------------------

class TestFileHash:
    def test_matches_known_md5(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_bytes(b"hello world")
        assert bi.file_hash(f) == hashlib.md5(b"hello world").hexdigest()

    def test_different_content_gives_different_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"content A")
        f2.write_bytes(b"content B")
        assert bi.file_hash(f1) != bi.file_hash(f2)

    def test_matches_for_content_larger_than_read_buffer(self, tmp_path):
        # file_hash reads in 65536-byte chunks - confirm chunked reads still
        # produce the same digest as hashing the whole buffer at once.
        content = b"x" * (65536 * 2 + 100)
        f = tmp_path / "big.bin"
        f.write_bytes(content)
        assert bi.file_hash(f) == hashlib.md5(content).hexdigest()


# ---------------------------------------------------------------------------
# get_embeddings
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, ok=True, json_data=None):
        self.ok = ok
        self._json = json_data or {}

    def json(self):
        return self._json


class TestGetEmbeddings:
    def test_successful_call_returns_embedding_vector(self, monkeypatch):
        monkeypatch.setattr(bi.requests, "post",
                             lambda *a, **k: FakeResponse(json_data={"embedding": [0.1, 0.2, 0.3]}))
        result = bi.get_embeddings(["some chunk"])
        assert result == [[0.1, 0.2, 0.3]]

    def test_non_ok_response_yields_empty_embedding(self, monkeypatch):
        monkeypatch.setattr(bi.requests, "post", lambda *a, **k: FakeResponse(ok=False))
        result = bi.get_embeddings(["chunk"])
        assert result == [[]]

    def test_request_exception_yields_empty_embedding_not_raise(self, monkeypatch):
        def raise_err(*a, **k):
            raise ConnectionError("ollama down")
        monkeypatch.setattr(bi.requests, "post", raise_err)
        result = bi.get_embeddings(["chunk"])
        assert result == [[]]

    def test_preserves_order_and_count_across_multiple_texts(self, monkeypatch):
        responses = iter([
            FakeResponse(json_data={"embedding": [1.0]}),
            FakeResponse(ok=False),
            FakeResponse(json_data={"embedding": [3.0]}),
        ])
        monkeypatch.setattr(bi.requests, "post", lambda *a, **k: next(responses))
        result = bi.get_embeddings(["a", "b", "c"])
        assert result == [[1.0], [], [3.0]]


# ---------------------------------------------------------------------------
# extract_pdf_text - fallback behavior when no PDF library is installed
# ---------------------------------------------------------------------------

class TestExtractPdfTextFallback:
    def test_missing_pdf_libraries_returns_empty_string_not_raise(self):
        # Neither pdfplumber nor PyPDF2 is installed in this environment,
        # matching the documented graceful-degradation path: no PDF library
        # available should never crash the index builder.
        assert bi.extract_pdf_text(Path("/nonexistent/doc.pdf")) == ""
