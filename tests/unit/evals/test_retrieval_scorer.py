"""Unit tests for evals/scorers/retrieval_scorer.py — standard RAG retrieval
metrics (Recall@1/5, MRR, NDCG@5, Precision@5) plus latency percentiles.

Metrics names must match the ablation report format so future harness runs
are directly comparable (docs/11 ADR decision 7)."""

from __future__ import annotations

import math

import pytest

from evals.scorers.retrieval_scorer import aggregate_scores, evaluate_retrieval, score_retrieved


class TestScoreRetrieved:
    def test_perfect_retrieval(self) -> None:
        m = score_retrieved(["doc-a", "doc-b", "doc-c"], ["doc-a", "doc-b", "doc-c"])
        # recall@k = hits in top-k / total relevant — with 3 relevant docs,
        # top-1 contains 1 of them.
        assert m["recall@1"] == pytest.approx(1 / 3)
        assert m["recall@5"] == 1.0
        assert m["mrr"] == 1.0
        assert m["ndcg@5"] == pytest.approx(1.0)
        # precision@k = hits / len(returned) — only 3 results returned, all hit
        assert m["precision@5"] == pytest.approx(1.0)

    def test_first_relevant_at_rank_2(self) -> None:
        m = score_retrieved(["doc-x", "doc-a"], ["doc-a", "doc-b"])
        assert m["recall@1"] == 0.0
        assert m["recall@5"] == 0.5
        assert m["mrr"] == pytest.approx(0.5)

    def test_empty_retrieved_scores_zero(self) -> None:
        m = score_retrieved([], ["doc-a"])
        assert m["recall@1"] == 0.0
        assert m["recall@5"] == 0.0
        assert m["mrr"] == 0.0
        assert m["ndcg@5"] == 0.0
        assert m["precision@5"] == 0.0

    def test_metric_names_match_ablation_format(self) -> None:
        m = score_retrieved(["doc-a"], ["doc-a"])
        assert set(m) == {"recall@1", "recall@5", "mrr", "ndcg@5", "precision@5"}

    def test_custom_k(self) -> None:
        m = score_retrieved(["doc-a", "doc-b"], ["doc-b"], k=2)
        assert m["recall@2"] == 1.0
        assert m["ndcg@2"] == pytest.approx(1.0 / math.log2(3))  # doc-b at rank 2


class TestAggregateScores:
    def test_averages_metrics(self) -> None:
        per_query = [
            {"recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0, "latency_ms": 100.0},
            {"recall@5": 0.5, "mrr": 0.5, "ndcg@5": 0.5, "latency_ms": 200.0},
        ]
        agg = aggregate_scores(per_query)
        assert agg["num_queries"] == 2
        assert agg["recall@5"] == pytest.approx(0.75)
        assert agg["mrr"] == pytest.approx(0.75)
        assert agg["ndcg@5"] == pytest.approx(0.75)
        # Percentile algorithm matches run_ablation (idx = int(n*pct) - 1):
        # with n=2 both percentiles index sorted[0].
        assert agg["p50_latency_ms"] == pytest.approx(100.0)
        assert agg["p95_latency_ms"] == pytest.approx(100.0)

    def test_empty_input(self) -> None:
        agg = aggregate_scores([])
        assert agg["num_queries"] == 0
        assert agg["recall@5"] == 0.0
        assert "p50_latency_ms" in agg

    def test_single_query_latency(self) -> None:
        agg = aggregate_scores([{"recall@5": 1.0, "mrr": 1.0, "latency_ms": 42.0}])
        assert agg["p50_latency_ms"] == pytest.approx(42.0)
        assert agg["p95_latency_ms"] == pytest.approx(42.0)

    def test_queries_without_latency(self) -> None:
        """Latency is optional — a batch mode can score without timing."""
        agg = aggregate_scores([{"recall@5": 1.0, "mrr": 1.0}])
        assert agg["num_queries"] == 1
        assert agg["recall@5"] == pytest.approx(1.0)
        assert agg["p50_latency_ms"] == 0.0


class TestEvaluateRetrieval:
    def test_end_to_end_with_fake_retriever(self) -> None:
        def fake_retrieve(query: str) -> list[dict]:
            return [{"source": "doc-a", "chunk_id": f"{query}-c1"}]

        queries = [
            {"query": "q1", "relevant_docs": ["doc-a"]},
            {"query": "q2", "relevant_docs": ["doc-a", "doc-b"]},
        ]
        report = evaluate_retrieval(queries, fake_retrieve)
        assert report["num_queries"] == 2
        assert report["recall@5"] == pytest.approx(0.75)  # q1: 1.0, q2: 0.5
        assert report["per_query"][0]["query"] == "q1"
        assert report["per_query"][0]["retrieved_ids"] == ["doc-a"]
        assert report["per_query"][0]["latency_ms"] >= 0.0

    def test_empty_queries(self) -> None:
        report = evaluate_retrieval([], lambda q: [])
        assert report["num_queries"] == 0
        assert report["recall@5"] == 0.0
