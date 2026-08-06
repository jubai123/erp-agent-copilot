"""Tests for KnowledgeDocument and DocumentChunk models."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db


@pytest.fixture(scope="module")
def _init_db(ensure_test_database: str) -> None:
    import erp_copilot.domain.entities  # noqa: F401

    settings = Settings(
        database_url=ensure_test_database,
        llm_api_key="sk-test",
    )
    init_db(settings)
    Base.metadata.create_all(get_engine())


@pytest.fixture(autouse=True)
def _clean_tables(_init_db: None) -> None:
    session = get_session()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(text(f"DELETE FROM {table.name} CASCADE"))
    session.commit()


class TestKnowledgeDocument:
    def test_create_document(self) -> None:
        from erp_copilot.domain.entities import KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-kd")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="docs/product.md",
            content_hash="abc123",
            status="PENDING",
            version=1,
        )
        session.add(doc)
        session.commit()

        assert doc.id is not None
        assert doc.source == "docs/product.md"
        assert doc.status == "PENDING"
        assert doc.version == 1

    def test_document_belongs_to_tenant(self) -> None:
        from erp_copilot.domain.entities import KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-kd-tenant")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="spec.md",
            content_hash="def456",
            status="RUNNING",
            version=1,
        )
        session.add(doc)
        session.commit()

        assert doc.tenant_id == tenant.id
        assert doc.tenant is not None
        assert doc.tenant.name == "Test"

    def test_status_default(self) -> None:
        from erp_copilot.domain.entities import KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-kd-default")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="test.md",
            content_hash="ghi789",
            version=1,
        )
        session.add(doc)
        session.commit()

        assert doc.status == "PENDING"

    def test_version_increment_on_update(self) -> None:
        from erp_copilot.domain.entities import KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-kd-ver")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="rules.md",
            content_hash="v1-hash",
            status="COMPLETED",
            version=1,
        )
        session.add(doc)
        session.flush()

        # Simulate a version update
        doc.version = 2
        doc.content_hash = "v2-hash"
        session.commit()

        session.refresh(doc)
        assert doc.version == 2
        assert doc.content_hash == "v2-hash"

    def test_cascade_delete_chunks(self) -> None:
        from erp_copilot.domain.entities import DocumentChunk, KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-cascade")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="cascade.md",
            content_hash="cascade",
            version=1,
        )
        session.add(doc)
        session.flush()

        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_index=0,
            content="Test content",
            section_path="[]",
            char_count=12,
        )
        session.add(chunk)
        session.commit()

        chunk_id = chunk.id
        session.delete(doc)
        session.commit()

        assert session.get(DocumentChunk, chunk_id) is None


class TestDocumentChunk:
    def test_create_chunk(self) -> None:
        from erp_copilot.domain.entities import DocumentChunk, KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-chunk")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="chunk-test.md",
            content_hash="chunk-hash",
            version=1,
        )
        session.add(doc)
        session.flush()

        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_index=0,
            content="# Section 1\n\nBody text here.",
            section_path='["Section 1"]',
            char_count=28,
        )
        session.add(chunk)
        session.commit()

        assert chunk.id is not None
        assert chunk.document_id == doc.id
        assert chunk.chunk_index == 0
        assert chunk.content == "# Section 1\n\nBody text here."

    def test_unique_document_chunk_index(self) -> None:
        from erp_copilot.domain.entities import DocumentChunk, KnowledgeDocument, Tenant

        session = get_session()
        tenant = Tenant(name="Test", slug="test-uniq-idx")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="unique.md",
            content_hash="uniq-hash",
            version=1,
        )
        session.add(doc)
        session.flush()

        c0 = DocumentChunk(
            document_id=doc.id, chunk_index=0, content="A", section_path="[]", char_count=1
        )
        session.add(c0)
        session.commit()

        c0_dup = DocumentChunk(
            document_id=doc.id, chunk_index=0, content="B", section_path="[]", char_count=1
        )
        session.add(c0_dup)
        with pytest.raises(IntegrityError):
            session.commit()
