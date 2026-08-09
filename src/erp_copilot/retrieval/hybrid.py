"""Hybrid retrieval: Reciprocal Rank Fusion (RRF) of semantic + keyword results.

Phase 3.8 — fuses :func:`search_similar` and :func:`keyword_search`
results into a single ranked list.
"""

from __future__ import annotations


def rrf_fuse(
    semantic_results: list[dict],
    keyword_results: list[dict],
    k: int = 60,
    top_k: int = 20,
) -> list[dict]:
    """Fuse two ranked result sets using Reciprocal Rank Fusion.

    RRF score for a document *d*:
        score(d) = Σ 1 / (k + rank_i(d))

    where *k* dampens the rank advantage (default 60) and *rank_i*
    is the 1-indexed position of *d* in result set *i* (or 0 if absent).

    Returns *top_k* results ordered by RRF score descending.
    """
    if not semantic_results and not keyword_results:
        return []

    # Map chunk_id → merged dict + accumulator
    merged: dict[str, dict] = {}
    scores: dict[str, float] = {}

    for rank_index, r in enumerate(semantic_results):
        cid = r["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank_index + 1)
        if cid not in merged:
            merged[cid] = {
                "chunk_id": cid,
                "content": r["content"],
                "section_path": r["section_path"],
                "char_count": r["char_count"],
                "source": r["source"],
            }

    for rank_index, r in enumerate(keyword_results):
        cid = r["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank_index + 1)
        if cid not in merged:
            merged[cid] = {
                "chunk_id": cid,
                "content": r["content"],
                "section_path": r["section_path"],
                "char_count": r["char_count"],
                "source": r["source"],
            }

    # Sort by RRF score desc, then truncate
    sorted_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    for cid in sorted_ids[:top_k]:
        merged[cid]["rrf_score"] = round(scores[cid], 6)

    return [merged[cid] for cid in sorted_ids[:top_k]]
