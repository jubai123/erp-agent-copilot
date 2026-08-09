"""Retrieval evaluation metrics: Recall@K, MRR, NDCG@K.

Phase 3.12 — standard information-retrieval metrics for measuring
retrieval pipeline quality.
"""

from __future__ import annotations

import math


def recall_at_k(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    k: int | None = None,
) -> float:
    """Fraction of relevant documents retrieved in the top *k* results.

    recall@k = |retrieved[:k] ∩ relevant| / |relevant|

    When *k* is None, all retrieved results are considered.
    """
    if not relevant_ids:
        return 0.0

    considered = retrieved_ids[:k] if k is not None else retrieved_ids
    hits = sum(1 for rid in considered if rid in relevant_ids)
    return hits / len(relevant_ids)


def mean_reciprocal_rank(
    retrieved_ids: list[str],
    relevant_ids: set[str],
) -> float:
    """Reciprocal rank of the first relevant result.

    MRR = 1 / rank_of_first_relevant

    Returns 0.0 if no relevant document is found.
    """
    if not relevant_ids:
        return 0.0

    for i, rid in enumerate(retrieved_ids, start=1):
        if rid in relevant_ids:
            return 1.0 / i
    return 0.0


def ndcg_at_k(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    graded_relevance: dict[str, int] | None = None,
    k: int | None = None,
) -> float:
    """Normalized Discounted Cumulative Gain at *k*.

    Uses binary relevance (1/0) unless *graded_relevance* is provided.
    NDCG = DCG / IDCG.  IDCG is the ideal ordering (all relevant docs first).
    """
    if not relevant_ids:
        return 0.0

    considered = retrieved_ids[:k] if k is not None else retrieved_ids
    if not considered:
        return 0.0

    rel_map = graded_relevance or {}

    # DCG
    dcg = 0.0
    for i, rid in enumerate(considered, start=1):
        if rid not in relevant_ids:
            continue
        gain = rel_map.get(rid, 1)
        dcg += (gain) / math.log2(i + 1)

    # IDCG (ideal: all relevant documents at top, sorted by gain desc)
    ideal_gains = sorted(
        [rel_map.get(r, 1) for r in relevant_ids],
        reverse=True,
    )
    ideal_gains = ideal_gains[: len(considered)]
    idcg = sum(gain / math.log2(i + 1) for i, gain in enumerate(ideal_gains, start=1))
    if idcg == 0.0:
        return 0.0

    return dcg / idcg


def precision_at_k(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    k: int | None = None,
) -> float:
    """Fraction of retrieved documents in the top *k* that are relevant.

    precision@k = |retrieved[:k] ∩ relevant| / k
    """
    considered = retrieved_ids[:k] if k is not None else retrieved_ids
    if not considered:
        return 0.0

    hits = sum(1 for rid in considered if rid in relevant_ids)
    return hits / len(considered)
