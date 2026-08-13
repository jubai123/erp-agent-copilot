"""Knowledge search API route — full retrieval pipeline endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

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
from erp_copilot.security.dependencies import Actor, require_scope

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])

# Reading the knowledge base requires the knowledge:erp:read scope (docs/06
# §4); the tenant comes from the auth context, never from the body (docs/07
# §10).
_REQUIRED_SCOPE = require_scope("knowledge:erp:read")


def _build_providers() -> tuple[EmbeddingProvider, Reranker]:
    """Resolve embedding/rerank providers from Settings.

    A configured API key uses the live service; otherwise the deterministic
    implementations keep the endpoint usable offline (tests, local demos).
    """
    settings = Settings()  # type: ignore[call-arg]
    return build_embedding_provider(settings), build_reranker(settings)


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    body: KnowledgeSearchRequest,
    actor: Actor = Depends(_REQUIRED_SCOPE),
) -> KnowledgeSearchResponse:
    """Search the knowledge base with the real retrieval pipeline.

    Executes: embed → vector search → keyword search → RRF fusion → rerank →
    citations. The actor must hold the ``knowledge:erp:read`` scope and the
    body tenant must match the authenticated identity — a body that names
    another tenant is a cross-tenant attempt (docs/07 §10). Tenant isolation
    comes from the authenticated tenant, which the retrieval stages enforce
    by joining through knowledge_documents.
    """
    if body.tenant_id != actor.tenant_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Body tenant '{body.tenant_id}' does not match "
                f"authenticated tenant '{actor.tenant_id}'"
            ),
        )
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
