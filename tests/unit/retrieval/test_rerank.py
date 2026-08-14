"""Tests for cross-encoder reranking (Phase 3.9)."""

from __future__ import annotations

import httpx
import pytest
import respx

from erp_copilot.retrieval.rerank import (
    DashScopeReranker,
    Reranker,
    rerank_results,
)

RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"


def _ok_response(index: int = 0, score: float = 0.9) -> httpx.Response:
    return httpx.Response(
        200,
        json={"output": {"results": [{"index": index, "relevance_score": score}]}},
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


class TestDashScopeRerankerRetry:
    """Transient failures (5xx, 429, transport errors) retry; permanent 4xx do not."""

    @pytest.fixture()
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)

    @respx.mock
    def test_retries_transient_5xx_then_succeeds(self, no_sleep: None) -> None:
        route = respx.post(RERANK_URL)
        route.side_effect = [httpx.Response(500, json={"error": "boom"}), _ok_response()]

        result = DashScopeReranker(api_key="sk-test").rerank("q", ["doc"])

        assert route.call_count == 2
        assert result == [(0, 0.9)]

    @respx.mock
    def test_retries_transport_error_then_succeeds(self, no_sleep: None) -> None:
        route = respx.post(RERANK_URL)
        route.side_effect = [httpx.ConnectError("conn reset"), _ok_response()]

        result = DashScopeReranker(api_key="sk-test").rerank("q", ["doc"])

        assert route.call_count == 2
        assert result == [(0, 0.9)]

    @respx.mock
    def test_permanent_4xx_raises_without_retry(self, no_sleep: None) -> None:
        route = respx.post(RERANK_URL)
        route.side_effect = [httpx.Response(401, json={"error": "invalid key"})]

        with pytest.raises(RuntimeError, match="401"):
            DashScopeReranker(api_key="sk-bad").rerank("q", ["doc"])

        assert route.call_count == 1

    @respx.mock
    def test_exhausts_retries_then_raises(self, no_sleep: None) -> None:
        route = respx.post(RERANK_URL)
        route.side_effect = [
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(503, json={"error": "still down"}),
            httpx.Response(500, json={"error": "final"}),
        ]

        with pytest.raises(RuntimeError, match="500"):
            DashScopeReranker(api_key="sk-test").rerank("q", ["doc"])

        assert route.call_count == 3


class TestMinRelevance:
    """Only calibrated rerankers expose a relevance gate for refusal decisions."""

    def test_dashscope_reranker_sets_threshold(self) -> None:
        assert DashScopeReranker(api_key="sk-test").min_relevance == 0.5

    def test_deterministic_reranker_has_no_threshold(self) -> None:
        from erp_copilot.retrieval.pipeline import DeterministicReranker

        assert DeterministicReranker().min_relevance is None

    def test_cross_encoder_reranker_has_no_threshold(self) -> None:
        from erp_copilot.retrieval.rerank import CrossEncoderReranker

        # Logit scores are uncalibrated — no meaningful absolute threshold.
        assert CrossEncoderReranker.min_relevance is None
