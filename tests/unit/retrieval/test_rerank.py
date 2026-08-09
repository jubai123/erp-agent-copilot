"""Tests for cross-encoder reranking (Phase 3.9)."""

from __future__ import annotations

from erp_copilot.retrieval.rerank import (
    Reranker,
    rerank_results,
)


class _DummyReranker:
    """Returns scores based on query-document keyword overlap ratio."""

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        results = []
        for i, doc in enumerate(documents):
            q_words = set(query.lower().split())
            d_words = set(doc.lower().split())
            overlap = len(q_words & d_words)
            score = overlap / max(len(q_words), 1)
            results.append((i, round(score, 4)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results


def _r(chunk_id: str, content: str, **kw):
    return {
        "chunk_id": chunk_id,
        "content": content,
        "section_path": kw.pop("section_path", []),
        "char_count": len(content),
        "source": "test.md",
        "rrf_score": 0.05,
    }


class TestRerankResults:
    def test_reranks_by_relevance(self) -> None:
        query = "order state machine transition"
        documents = [
            _r("a", "order state machine legal transitions CREATED to CONFIRMED"),
            _r("b", "supplier filtering rules sort by rating delivery time"),
            _r("c", "order creation flow includes state machine validation"),
        ]
        reranker = _DummyReranker()

        result = rerank_results(query, documents, reranker, top_k=3)
        assert result[0]["chunk_id"] == "a"
        assert result[1]["chunk_id"] == "c"
        assert result[2]["chunk_id"] == "b"

    def test_empty_documents_returns_empty(self) -> None:
        result = rerank_results("query", [], _DummyReranker())
        assert result == []

    def test_top_k_truncation(self) -> None:
        query = "test query"
        documents = [_r(str(i), f"doc{i}") for i in range(10)]

        result = rerank_results(query, documents, _DummyReranker(), top_k=3)
        assert len(result) == 3

    def test_adds_rerank_score_to_output(self) -> None:
        query = "test"
        documents = [_r("a", "test document")]

        result = rerank_results(query, documents, _DummyReranker())
        assert "rerank_score" in result[0]
        assert isinstance(result[0]["rerank_score"], float)

    def test_preserves_original_fields(self) -> None:
        query = "test"
        documents = [_r("a", content="original", section_path=["H1"])]

        result = rerank_results(query, documents, _DummyReranker())
        r = result[0]
        assert r["content"] == "original"
        assert r["section_path"] == ["H1"]
        assert "rrf_score" not in r  # cleaned

    def test_conforms_to_protocol(self) -> None:
        reranker = _DummyReranker()
        assert isinstance(reranker, Reranker)
