"""Knowledge search API route — full retrieval pipeline endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from apps.api.schemas.knowledge import (
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    SearchResultItem,
)
from erp_copilot.retrieval.citation import assemble_citations

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(body: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
    """Search the knowledge base with full retrieval pipeline.

    Executes: keyword search → semantic search → RRF fusion → rerank → citations.
    Falls back to a stub implementation when the database is not populated.
    """
    # TODO: When database is populated, wire the full pipeline:
    #   embedding = embed_chunks([...], provider)
    #   vec_results = search_similar(session, embedding, body.tenant_id, top_k=10)
    #   fts_results = keyword_search(session, body.query, body.tenant_id, top_k=10)
    #   fused = rrf_fuse(vec_results, fts_results)
    #   reranked = rerank_results(body.query, fused, reranker, top_k=body.top_k)
    #   citations = assemble_citations(reranked)
    #
    # For now, return an empty result set so the endpoint structure is testable.

    results: list[dict] = []
    citations = assemble_citations(results)

    return KnowledgeSearchResponse(
        results=[SearchResultItem(**r) for r in results],
        citations=citations,
    )
