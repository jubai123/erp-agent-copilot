"""Standard RAG retrieval scorer.

Independent, reusable metrics layer for the L2 retrieval evaluation
(ADR decision 7).  Metric names and aggregation follow the ablation report
format (Recall@1/5, MRR, NDCG@5, Precision@5, P50/P95 latency) so harness
runs are directly comparable to the published ablation numbers.

The metric formulas live in erp_copilot.retrieval.metrics; this module
only orchestrates scoring and aggregation.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from erp_copilot.retrieval.metrics import (
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

RetrieveFn = Callable[[str], list[dict]]


def score_retrieved(retrieved_ids: list[str], relevant_ids: list[str], k: int = 5) -> dict:
    """Score a single query's retrieval against the relevant document set."""
    relevant = set(relevant_ids)
    return {
        "recall@1": recall_at_k(retrieved_ids, relevant, k=1),
        f"recall@{k}": recall_at_k(retrieved_ids, relevant, k=k),
        "mrr": mean_reciprocal_rank(retrieved_ids, relevant),
        f"ndcg@{k}": ndcg_at_k(retrieved_ids, relevant, k=k),
        f"precision@{k}": precision_at_k(retrieved_ids, relevant, k=k),
    }


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(int(len(sorted_values) * pct) - 1, 0)
    return sorted_values[idx]


_METRIC_KEYS = ("recall@1", "recall@5", "mrr", "ndcg@5", "precision@5")


def aggregate_scores(per_query: list[dict]) -> dict:
    """Aggregate per-query scores into the ablation-style summary.

    Only metric keys present in the per-query records are aggregated
    (missing = not measured in this mode, never treated as zero).  Latency
    is optional; queries without it contribute nothing to the percentiles.
    """
    n = len(per_query)
    if n == 0:
        return {
            "num_queries": 0,
            **dict.fromkeys(_METRIC_KEYS, 0.0),
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
        }

    latencies = sorted(q.get("latency_ms", 0.0) for q in per_query)
    result: dict = {
        "num_queries": n,
        "p50_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
    }
    for key in _METRIC_KEYS:
        if key in per_query[0]:
            result[key] = sum(q[key] for q in per_query) / n
    return result


def evaluate_retrieval(queries: list[dict], retrieve_fn: RetrieveFn, k: int = 5) -> dict:
    """Run *retrieve_fn* for each query and produce a full score report.

    Each query is ``{"query": str, "relevant_docs": [str, ...]}``.
    *retrieve_fn* returns result dicts; their ``source`` field identifies
    the document (matching the ablation script's doc-level evaluation).
    """
    per_query: list[dict] = []
    for q in queries:
        query_text = q["query"]
        relevant = q["relevant_docs"]

        t0 = time.perf_counter()
        results = retrieve_fn(query_text)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        retrieved_ids: list[str] = []
        seen: set[str] = set()
        for r in results:
            src = r["source"]
            if src not in seen:
                seen.add(src)
                retrieved_ids.append(src)

        metrics = score_retrieved(retrieved_ids, relevant, k=k)
        metrics["query"] = query_text
        metrics["relevant_ids"] = list(relevant)
        metrics["retrieved_ids"] = retrieved_ids
        metrics["latency_ms"] = round(elapsed_ms, 2)
        per_query.append(metrics)

    report = aggregate_scores(per_query)
    report["per_query"] = per_query
    return report
