"""E2E integration test for the full retrieval pipeline.

Verifies: ingestion → embedding → storage → search → fusion → citations.
Requires a local PostgreSQL with pgvector extension.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from erp_copilot.infrastructure.database import get_session


@pytest.fixture(scope="session")
def _ensure_pgvector(_init_db: None) -> None:
    """Ensure the pgvector extension is installed."""
    session = get_session()
    try:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def session(_init_db: None, _ensure_pgvector: None):
    """Return a database session for a single test."""
    s = get_session()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Stub embedding provider
# ---------------------------------------------------------------------------


class _DummyProvider:
    """Returns 1536-dim embeddings based on content hash for deterministic tests."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for t in texts:
            # Deterministic: each char's ordinal contributes to a base value
            base = float(sum(ord(c) for c in t) % 10) / 10.0
            result.append([(base + i * 0.001) % 1.0 for i in range(1536)])
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_tenant(session, tenant_id: str) -> None:
    """Create a tenant if it doesn't exist."""
    slug = tenant_id.replace("-", "_")
    session.execute(
        text(
            "INSERT INTO tenants (id, name, slug, is_active, created_at, updated_at) "
            "VALUES (:id, :name, :slug, true, now(), now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": tenant_id, "name": tenant_id, "slug": slug},
    )
    session.flush()


def _ingest_doc(session, tenant_id: str, source: str, content: str) -> str:
    """Insert a knowledge document with chunks and embeddings (full pipeline)."""
    from erp_copilot.domain.entities import KnowledgeDocument
    from erp_copilot.retrieval.embedding import embed_chunks
    from erp_copilot.retrieval.ingestion import hash_content, ingest_document
    from erp_copilot.retrieval.vector_store import store_embeddings

    # 0. Ensure tenant exists
    _ensure_tenant(session, tenant_id)

    # 1. Hash and dedup
    content_hash = hash_content(content)

    # 2. Create document record
    doc = KnowledgeDocument(
        tenant_id=tenant_id,
        source=source,
        content_hash=content_hash,
        status="COMPLETED",
        version=1,
    )
    session.add(doc)
    session.flush()

    # 3. Parse and chunk
    chunks = ingest_document(content, source)

    # 4. Embed
    provider = _DummyProvider()
    embedded = embed_chunks(chunks, provider)

    # 5. Store
    store_embeddings(session, doc.id, embedded)

    # 6. Commit so a different session (e.g. the API route's) can read it.
    session.commit()
    return doc.id


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


class TestRetrievalPipelineE2E:
    """End-to-end retrieval pipeline with real database."""

    SAMPLE_DOC = """# 订单状态机

## 合法转换

CREATED → CONFIRMED → SHIPPED → DELIVERED。

### 取消规则

CREATED 和 CONFIRMED 状态的订单可以无责取消。
SHIPPED 状态可取消但需提示物流拦截费用。
DELIVERED 状态不可取消，应引导用户走退货流程。
"""

    def test_full_pipeline_embed_and_search(self, session) -> None:
        """Chunk → Embed → Store → Vector Search → Citations."""
        from erp_copilot.retrieval.citation import assemble_citations
        from erp_copilot.retrieval.vector_store import search_similar

        # Ingest a document
        doc_id = _ingest_doc(session, "tenant-1", "order-lifecycle.md", self.SAMPLE_DOC)
        assert doc_id is not None

        # Search with a relevant embedding
        query_embedding = [0.5] * 1536
        results = search_similar(session, query_embedding, "tenant-1", top_k=5)
        assert len(results) >= 1
        assert all("content" in r for r in results)
        assert all("distance" in r for r in results)

        # Citations
        text = assemble_citations(results)
        assert "参考资料" in text
        assert "[1]" in text

    def test_tenant_isolation(self, session) -> None:
        """Different tenants see different results."""
        from erp_copilot.retrieval.vector_store import search_similar

        _ingest_doc(session, "tenant-a", "rules.md", "# 规则A\n\n内容A")
        _ingest_doc(session, "tenant-b", "rules.md", "# 规则B\n\n内容B")

        results_a = search_similar(session, [0.5] * 1536, "tenant-a", top_k=10)
        results_b = search_similar(session, [0.5] * 1536, "tenant-b", top_k=10)

        # Each tenant only sees their own documents
        sources_a = {r["source"] for r in results_a}
        sources_b = {r["source"] for r in results_b}
        assert sources_a == {"rules.md"}
        assert sources_b == {"rules.md"}
        # Content differs
        contents_a = {r["content"] for r in results_a}
        contents_b = {r["content"] for r in results_b}
        assert contents_a.isdisjoint(contents_b)

    def test_hybrid_fusion(self, session) -> None:
        """RRF fuses vector and keyword results correctly."""
        from erp_copilot.retrieval.hybrid import rrf_fuse
        from erp_copilot.retrieval.vector_store import search_similar

        _ingest_doc(session, "tenant-1", "order-rules.md", self.SAMPLE_DOC)

        vec_results = search_similar(session, [0.5] * 1536, "tenant-1", top_k=5)

        # For keyword results, use a mock since search_vector isn't populated
        keyword_results: list[dict] = []

        fused = rrf_fuse(vec_results, keyword_results, k=60, top_k=5)
        assert len(fused) > 0
        assert all("rrf_score" in r for r in fused)

    def test_rerank_and_citation_chain(self, session) -> None:
        """Full chain: search → rerank → citations."""
        from erp_copilot.retrieval.citation import assemble_citations
        from erp_copilot.retrieval.rerank import rerank_results
        from erp_copilot.retrieval.vector_store import search_similar

        _ingest_doc(session, "tenant-1", "lifecycle.md", self.SAMPLE_DOC)

        # Stage 1: Vector search
        vec_results = search_similar(session, [0.5] * 1536, "tenant-1", top_k=5)

        # Stage 2: Rerank with dummy reranker
        class _DummyReranker:
            def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
                return [(i, 1.0 / (i + 1)) for i in range(len(documents))]

        reranked = rerank_results("订单状态机", vec_results, _DummyReranker(), top_k=3)
        assert len(reranked) > 0
        assert all("rerank_score" in r for r in reranked)

        # Stage 3: Citations
        citations = assemble_citations(reranked)
        assert "参考资料" in citations
        assert "订单状态机" in citations

    def test_empty_query_returns_empty_pipeline(self, session) -> None:
        """Searching without ingested data returns empty."""
        from erp_copilot.retrieval.vector_store import search_similar

        results = search_similar(session, [0.1] * 1536, "tenant-empty", top_k=5)
        assert results == []


class TestKnowledgeSearchEndpoint:
    """POST /v1/knowledge/search runs the real pipeline against the DB.

    Providers are pinned to the deterministic implementations so the test
    exercises the full DB-backed chain without any external API call.
    """

    SAMPLE_DOC = """# 订单状态机

## 合法转换

CREATED → CONFIRMED → SHIPPED → DELIVERED。

### 取消规则

CREATED 和 CONFIRMED 状态的订单可以无责取消。
DELIVERED 状态不可取消，应引导用户走退货流程。
"""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch):
        from fastapi.testclient import TestClient

        from apps.api.main import create_app
        from erp_copilot.retrieval.pipeline import (
            DeterministicEmbeddingProvider,
            DeterministicReranker,
        )

        monkeypatch.setattr(
            "apps.api.routes.knowledge._build_providers",
            lambda: (DeterministicEmbeddingProvider(), DeterministicReranker()),
        )
        return TestClient(create_app())

    def test_search_returns_ingested_chunks(self, session, client) -> None:
        _ingest_doc(session, "tenant-e2e", "order-lifecycle.md", self.SAMPLE_DOC)

        response = client.post(
            "/v1/knowledge/search",
            json={"query": "订单状态", "tenant_id": "tenant-e2e"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["results"]
        assert any(r["source"] == "order-lifecycle.md" for r in body["results"])
        assert any(r["score"] > 0 for r in body["results"])
        assert "参考资料" in body["citations"]

    def test_search_respects_tenant_isolation(self, session, client) -> None:
        _ingest_doc(session, "tenant-a", "rules-a.md", "# 规则A\n\n内容A")
        _ingest_doc(session, "tenant-b", "rules-b.md", "# 规则B\n\n内容B")

        response = client.post(
            "/v1/knowledge/search",
            json={"query": "规则", "tenant_id": "tenant-a"},
        )

        sources = {r["source"] for r in response.json()["results"]}
        assert sources == {"rules-a.md"}

    def test_empty_knowledge_base_returns_empty(self, session, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "订单", "tenant_id": "tenant-empty"},
        )

        assert response.status_code == 200
        assert response.json()["results"] == []
        assert response.json()["citations"] == ""
