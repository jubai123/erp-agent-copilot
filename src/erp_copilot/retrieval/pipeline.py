"""Real retrieval pipeline runner — v1.1 pipeline 接线.

search_knowledge wires the delivered retrieval stages into a single runnable:
embed → vector search (pgvector) → keyword search (jieba FTS) → RRF fusion →
optional rerank, normalizing every result to a
{chunk_id, content, section_path, char_count, source, score} dict. Both the
HTTP route (apps/api/routes/knowledge.py) and the eval harness (evals/run_all.py)
reuse this one runner, so an API response and a benchmark number measure the
same chain.

The deterministic provider/reranker stand in for their network-backed
counterparts when no API key is configured, keeping the endpoint and the
eval offline and reproducible.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from erp_copilot.infrastructure.config import Settings
from erp_copilot.retrieval.embedding import EmbeddingProvider, OpenAIEmbeddingProvider
from erp_copilot.retrieval.fts import keyword_search
from erp_copilot.retrieval.hybrid import rrf_fuse
from erp_copilot.retrieval.rerank import DashScopeReranker, Reranker, rerank_results
from erp_copilot.retrieval.vector_store import search_similar


class DeterministicEmbeddingProvider:
    """Content-derived 1536-dim vectors — offline, reproducible, no API."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for t in texts:
            base = float(sum(ord(c) for c in t) % 10) / 10.0
            result.append([(base + i * 0.001) % 1.0 for i in range(1536)])
        return result


class DeterministicReranker:
    """Length-based rerank heuristic — offline stand-in for a cross-encoder."""

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        scored = [(i, 1.0 / max(len(d), 1)) for i, d in enumerate(documents)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Real OpenAI-compatible provider when a key is configured, else deterministic."""
    key = settings.llm_api_key.get_secret_value()
    if key:
        return OpenAIEmbeddingProvider(
            base_url=settings.llm_base_url,
            api_key=key,
            model=settings.embedding_model,
        )
    return DeterministicEmbeddingProvider()


def build_reranker(settings: Settings | None = None) -> Reranker:
    """DashScope reranker when a key is set, else the deterministic heuristic."""
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if api_key:
        return DashScopeReranker(api_key=api_key)
    return DeterministicReranker()


def search_knowledge(
    session: Session,
    query: str,
    tenant_id: str,
    top_k: int = 5,
    embedding_provider: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
) -> list[dict[str, Any]]:
    """Run the full retrieval pipeline for *query* inside *tenant_id*.

    Recalls 2× *top_k* candidates from each channel so fusion has headroom,
    then reranks to *top_k* when *reranker* is present. Results carry a
    ``score`` key: the rerank score when reranked, else the RRF score.
    """
    if embedding_provider is None:
        raise ValueError("embedding_provider is required for semantic retrieval")

    query_embedding = embedding_provider.embed([query])[0]
    candidate_k = max(top_k * 2, 20)
    vec_results = search_similar(session, query_embedding, tenant_id, top_k=candidate_k)
    kw_results = keyword_search(session, query, tenant_id, top_k=candidate_k)
    fused = rrf_fuse(vec_results, kw_results, k=60, top_k=candidate_k)

    if reranker is not None:
        ranked = rerank_results(query, fused, reranker, top_k=top_k)
        return [_normalize(r, "rerank_score") for r in ranked]
    return [_normalize(r, "rrf_score") for r in fused[:top_k]]


def _normalize(result: dict[str, Any], score_key: str) -> dict[str, Any]:
    """Map a stage-specific result dict onto the shared pipeline output shape."""
    return {
        "chunk_id": result["chunk_id"],
        "content": result["content"],
        "section_path": result["section_path"],
        "char_count": result["char_count"],
        "source": result["source"],
        "score": result[score_key],
    }
