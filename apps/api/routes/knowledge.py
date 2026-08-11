"""Knowledge search API route — full retrieval pipeline endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from apps.api.schemas.knowledge import (
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    SearchResultItem,
)
from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import get_session
from erp_copilot.retrieval.citation import assemble_citations
from erp_copilot.retrieval.embedding import EmbeddingProvider
from erp_copilot.retrieval.pipeline import (
    build_embedding_provider,
    build_reranker,
)
from erp_copilot.retrieval.pipeline import (
    search_knowledge as _pipeline_search,
)
from erp_copilot.retrieval.rerank import Reranker

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


def _build_providers() -> tuple[EmbeddingProvider, Reranker]:
    """Resolve embedding/rerank providers from Settings.

    A configured API key uses the live service; otherwise the deterministic
    implementations keep the endpoint usable offline (tests, local demos).
    """
    settings = Settings()
    return build_embedding_provider(settings), build_reranker(settings)


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(body: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
    """Search the knowledge base with the real retrieval pipeline.

    Executes: embed → vector search → keyword search → RRF fusion → rerank →
    citations. Tenant isolation comes from the request's tenant_id, which the
    retrieval stages enforce by joining through knowledge_documents.
    """
    session = get_session()
    try:
        embedding_provider, reranker = _build_providers()
        results = _pipeline_search(
            session,
            body.query,
            body.tenant_id,
            top_k=body.top_k,
            embedding_provider=embedding_provider,
            reranker=reranker,
        )
        return KnowledgeSearchResponse(
            results=[
                SearchResultItem(
                    chunk_id=r["chunk_id"],
                    content=r["content"],
                    source=r["source"],
                    section_path=r["section_path"],
                    score=r["score"],
                )
                for r in results
            ],
            citations=assemble_citations(results),
        )
    finally:
        session.close()
