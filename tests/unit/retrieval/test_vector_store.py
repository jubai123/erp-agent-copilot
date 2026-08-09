"""Tests for vector store and similarity search (Phase 3.6)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from erp_copilot.retrieval.ingestion import Chunk
from erp_copilot.retrieval.vector_store import search_similar, store_embeddings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(chunk_id: str, content: str, index: int) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        content=content,
        source_document="test.md",
        section_path=["Test"],
        chunk_index=index,
        char_count=len(content),
    )


def _fake_row(**kwargs):
    """Return a dict-like MagicMock that supports both .attr and ["key"] access."""
    m = MagicMock()
    m.configure_mock(**kwargs)
    m.__getitem__.side_effect = lambda k: getattr(m, k)
    return m


# ---------------------------------------------------------------------------
# store_embeddings tests
# ---------------------------------------------------------------------------


class TestStoreEmbeddings:
    def test_stores_embedding_and_updates_search_vector(self) -> None:
        session = MagicMock()
        doc_id = "doc-1"
        chunk = _make_chunk("chunk-1", "hello", 0)
        embedding = [0.1, 0.2, 0.3]

        store_embeddings(session, doc_id, [(chunk, embedding)])

        session.flush.assert_called_once()
        assert session.execute.call_count == 2

        # First call: INSERT embedding
        insert_call = session.execute.call_args_list[0]
        insert_params = insert_call[0][1]
        assert len(insert_params) == 1
        assert insert_params[0]["embedding"] == [0.1, 0.2, 0.3]
        assert insert_params[0]["chunk_id"] == "chunk-1"
        assert insert_params[0]["document_id"] == "doc-1"

        # Second call: UPDATE search_vector with jieba segmentation
        update_call = session.execute.call_args_list[1]
        update_params = update_call[0][1]
        assert len(update_params) == 1
        assert update_params[0]["chunk_id"] == "chunk-1"
        assert "segmented" in update_params[0]

    def test_empty_chunks_noop(self) -> None:
        session = MagicMock()
        store_embeddings(session, "doc-1", [])
        session.execute.assert_not_called()
        session.flush.assert_not_called()

    def test_batch_update_multiple_chunks(self) -> None:
        session = MagicMock()
        doc_id = "doc-1"
        chunks = [
            (_make_chunk("a", "alpha", 0), [1.0, 0.0]),
            (_make_chunk("b", "beta", 1), [0.0, 1.0]),
        ]

        store_embeddings(session, doc_id, chunks)
        params_list = session.execute.call_args[0][1]
        assert len(params_list) == 2


# ---------------------------------------------------------------------------
# search_similar tests
# ---------------------------------------------------------------------------


class TestSearchSimilar:
    def test_returns_chunks_ordered_by_distance(self) -> None:
        session = MagicMock()

        session.execute.return_value.mappings.return_value.all.return_value = [
            _fake_row(
                chunk_id="a",
                content="alpha chunk",
                section_path='["Test"]',
                char_count=12,
                source="doc.md",
                distance=0.15,
            ),
            _fake_row(
                chunk_id="b",
                content="beta chunk",
                section_path='["Test"]',
                char_count=11,
                source="doc.md",
                distance=0.45,
            ),
        ]

        results = search_similar(session, [0.1, 0.2, 0.3], "tenant-1", top_k=5)

        assert len(results) == 2
        assert results[0]["chunk_id"] == "a"
        assert results[0]["distance"] == 0.15
        assert results[1]["chunk_id"] == "b"
        assert results[1]["content"] == "beta chunk"

    def test_enforces_tenant_isolation(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []

        search_similar(session, [0.1], "tenant-x", top_k=3)
        params = session.execute.call_args[0][1]
        assert params["tenant_id"] == "tenant-x"

    def test_empty_embedding_raises(self) -> None:
        session = MagicMock()
        with pytest.raises(ValueError, match="query_embedding must not be empty"):
            search_similar(session, [], "t1", top_k=3)

    def test_default_top_k_is_10(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []

        search_similar(session, [0.1, 0.2], "t1")
        params = session.execute.call_args[0][1]
        assert params["top_k"] == 10

    def test_field_types_in_result(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            _fake_row(
                chunk_id="c1",
                content="test content",
                section_path='["H1", "H2"]',
                char_count=100,
                source="source.md",
                distance=0.05,
            )
        ]

        results = search_similar(session, [0.1], "t1")
        r = results[0]
        assert isinstance(r["chunk_id"], str)
        assert isinstance(r["content"], str)
        assert isinstance(r["section_path"], list)
        assert r["section_path"] == ["H1", "H2"]
        assert isinstance(r["char_count"], int)
        assert isinstance(r["source"], str)
        assert isinstance(r["distance"], float)
