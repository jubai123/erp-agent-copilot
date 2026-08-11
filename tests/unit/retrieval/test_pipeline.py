"""Unit tests for the real retrieval pipeline runner — v1.1 pipeline 接线.

search_knowledge wires the four retrieval stages into one runnable:
embed → vector search → FTS search → RRF fusion → (optional) rerank, and
normalizes every result to a {chunk_id, content, section_path, char_count,
source, score} dict. The DB boundary (search_similar / keyword_search) is
mocked; fusion and rerank run for real so the wiring itself is verified.
"""

from __future__ import annotations

import pytest

from erp_copilot.retrieval.pipeline import search_knowledge


class _FakeProvider:
    """Records embed calls and returns a fixed vector per text."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeReranker:
    """Records rerank calls; prefers documents earlier in the list."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        self.calls.append((query, documents))
        return [(i, 1.0 / (i + 1)) for i in range(len(documents))]


def _vec_results() -> list[dict]:
    return [
        {
            "chunk_id": "c1",
            "content": "创建订单需提供商品、数量与供应商",
            "section_path": ["订单流程"],
            "char_count": 16,
            "source": "process-order-creation",
            "distance": 0.1,
        },
        {
            "chunk_id": "c2",
            "content": "订单状态为 CREATED 与 CANCELLED",
            "section_path": ["订单规则"],
            "char_count": 18,
            "source": "rule-order-lifecycle",
            "distance": 0.2,
        },
    ]


def _kw_results() -> list[dict]:
    return [
        {
            "chunk_id": "c2",
            "content": "订单状态为 CREATED 与 CANCELLED",
            "section_path": ["订单规则"],
            "char_count": 18,
            "source": "rule-order-lifecycle",
            "rank": 0.9,
        },
        {
            "chunk_id": "c3",
            "content": "取消订单需先查订单再确认",
            "section_path": ["订单流程"],
            "char_count": 15,
            "source": "process-order-cancellation",
            "rank": 0.5,
        },
    ]


@pytest.fixture()
def mock_db_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the two SQL-backed retrieval stages with fixed results."""

    def fake_similar(session, query_embedding, tenant_id, top_k):
        return _vec_results()

    def fake_keyword(session, query, tenant_id, top_k):
        return _kw_results()

    monkeypatch.setattr("erp_copilot.retrieval.pipeline.search_similar", fake_similar)
    monkeypatch.setattr("erp_copilot.retrieval.pipeline.keyword_search", fake_keyword)


class TestSearchKnowledge:
    def test_embeds_query_and_reads_both_channels(
        self, mock_db_boundary: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _FakeProvider()
        session = object()

        search_knowledge(
            session,
            "如何创建订单",
            "tenant-1",
            top_k=5,
            embedding_provider=provider,
        )

        assert provider.calls == [["如何创建订单"]]

    def test_fuses_and_reranks_with_reranker(self, mock_db_boundary: None) -> None:
        provider = _FakeProvider()
        reranker = _FakeReranker()

        results = search_knowledge(
            object(),
            "如何创建订单",
            "tenant-1",
            top_k=5,
            embedding_provider=provider,
            reranker=reranker,
        )

        # c2 matches both channels, so fusion ranks it first; rerank keeps it.
        assert results[0]["chunk_id"] == "c2"
        assert results[0]["score"] == 1.0
        assert reranker.calls == [
            (
                "如何创建订单",
                [
                    "订单状态为 CREATED 与 CANCELLED",
                    "创建订单需提供商品、数量与供应商",
                    "取消订单需先查订单再确认",
                ],
            )
        ]

    def test_falls_back_to_rrf_score_without_reranker(self, mock_db_boundary: None) -> None:
        results = search_knowledge(
            object(),
            "如何创建订单",
            "tenant-1",
            top_k=5,
            embedding_provider=_FakeProvider(),
        )

        assert results[0]["chunk_id"] == "c2"
        # rrf_fuse rounds scores to 6 decimals.
        assert results[0]["score"] == pytest.approx(1 / 61 + 1 / 62, abs=1e-6)
        assert "rerank_score" not in results[0]

    def test_normalizes_output_keys(self, mock_db_boundary: None) -> None:
        results = search_knowledge(
            object(),
            "如何创建订单",
            "tenant-1",
            top_k=5,
            embedding_provider=_FakeProvider(),
            reranker=_FakeReranker(),
        )

        assert set(results[0].keys()) == {
            "chunk_id",
            "content",
            "section_path",
            "char_count",
            "source",
            "score",
        }
        assert results[0]["section_path"] == ["订单规则"]

    def test_returns_empty_when_no_fused_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("erp_copilot.retrieval.pipeline.search_similar", lambda *a, **k: [])
        monkeypatch.setattr("erp_copilot.retrieval.pipeline.keyword_search", lambda *a, **k: [])

        results = search_knowledge(
            object(),
            "汇率是多少",
            "tenant-1",
            top_k=5,
            embedding_provider=_FakeProvider(),
        )

        assert results == []

    def test_requires_embedding_provider(self, mock_db_boundary: None) -> None:
        with pytest.raises(ValueError, match="embedding_provider"):
            search_knowledge(object(), "如何创建订单", "tenant-1", top_k=5)
