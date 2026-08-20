"""Integration tests for the vocabulary approval API (Phase B).

GET /v1/vocabulary/proposals lists PENDING proposals; POST
/v1/vocabulary/proposals/{id}/decide approves or rejects one — approval writes
a vocabulary_terms row, flips the proposal, appends an audit_logs row and
invalidates the loader cache; GET /v1/vocabulary/terms lists the approved
runtime terms. The decide route requires the vocabulary:write scope, and the
batch task (with a stubbed LLM) turns captured observations into PENDING
proposals.

Tests run against the dedicated test database (tests/conftest.py).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import (
    AuditLog,
    Role,
    RoleScope,
    Tenant,
    User,
    UserRole,
    VocabularyObservation,
    VocabularyProposal,
    VocabularyTerm,
)
from erp_copilot.infrastructure.database import get_session
from erp_copilot.vocabulary import loader
from erp_copilot.vocabulary.loader import get_catalog


@pytest.fixture(autouse=True)
def _reset_vocab_cache():
    """The loader cache is process-global; leave it clean between tests so a
    flag-on test's merged catalog can't leak into a later flag-off test."""
    yield
    loader.invalidate()


def _create_tenant(name: str, slug: str) -> str:
    session = get_session()
    try:
        tenant = Tenant(name=name, slug=slug)
        session.add(tenant)
        session.commit()
        return str(tenant.id)
    finally:
        session.close()


def _make_operator(session: Session, tenant_id: str) -> str:
    """Create an active user holding the vocabulary:write scope."""
    role = Role(tenant_id=tenant_id, name="vocab-operator")
    session.add(role)
    session.flush()
    session.add(RoleScope(role_id=role.id, resource="vocabulary", action="write"))
    user = User(
        tenant_id=tenant_id,
        email="operator@example.com",
        hashed_password="x",
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user.id


def _make_readonly_user(session: Session, tenant_id: str) -> str:
    """Create a user WITHOUT vocabulary:write (for the 403 path)."""
    role = Role(tenant_id=tenant_id, name="reader")
    session.add(role)
    session.flush()
    session.add(RoleScope(role_id=role.id, resource="product", action="read"))
    user = User(
        tenant_id=tenant_id,
        email="reader@example.com",
        hashed_password="x",
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user.id


def _seed_proposal(
    session: Session,
    *,
    canonical: str = "榴莲",
    vocab_type: str = "product",
    proposal_key: str | None = None,
) -> VocabularyProposal:
    proposal = VocabularyProposal(
        proposal_key=proposal_key or f"key-{canonical}",
        vocab_type=vocab_type,
        canonical=canonical,
        aliases="[]",
        evidence='[{"query": "榴莲多少钱"}]',
        confidence=0.9,
    )
    session.add(proposal)
    session.commit()
    return proposal


def _headers(tenant_id: str, user_id: str) -> dict[str, str]:
    return {"X-Tenant-ID": tenant_id, "X-User-ID": user_id}


class TestDecide:
    def test_approve_creates_term_flips_proposal_audits_and_invalidates(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Approve Vocab", "approve-vocab")
        operator = _make_operator(session, tenant_id)
        proposal = _seed_proposal(session, canonical="榴莲")

        catalog_before = get_catalog()
        client = TestClient(create_app())
        response = client.post(
            f"/v1/vocabulary/proposals/{proposal.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "APPROVED"
        assert data["canonical"] == "榴莲"

        session.refresh(proposal)
        assert proposal.status == "APPROVED"
        assert proposal.decided_by == "operator"

        term = (
            session.query(VocabularyTerm)
            .filter_by(vocab_type="product", canonical="榴莲")
            .first()
        )
        assert term is not None
        assert term.source == "approved_proposal"
        assert term.is_active is True

        audit = (
            session.query(AuditLog)
            .filter_by(resource=f"vocabulary_proposal:{proposal.id}")
            .first()
        )
        assert audit is not None
        assert audit.action == "approve"
        assert audit.result == "APPROVED"
        assert audit.actor == "operator"

        # The decision invalidated the loader cache: a fresh get_catalog() call
        # rebuilds a new object rather than returning the cached one.
        assert get_catalog() is not catalog_before

    def test_reject_flips_proposal_without_creating_term(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Reject Vocab", "reject-vocab")
        operator = _make_operator(session, tenant_id)
        proposal = _seed_proposal(session, canonical="榴莲")

        client = TestClient(create_app())
        response = client.post(
            f"/v1/vocabulary/proposals/{proposal.id}/decide",
            json={"decision": "REJECTED", "decided_by": "operator", "reason": "非业务词"},
            headers=_headers(tenant_id, operator),
        )
        assert response.status_code == 200
        session.refresh(proposal)
        assert proposal.status == "REJECTED"
        assert proposal.decision_reason == "非业务词"
        assert (
            session.query(VocabularyTerm)
            .filter_by(vocab_type="product", canonical="榴莲")
            .first()
            is None
        )

    def test_approve_duplicate_term_returns_409(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Dup Vocab", "dup-vocab")
        operator = _make_operator(session, tenant_id)
        # Two different proposals proposing the same canonical -> the second
        # approve collides on the (vocab_type, canonical) unique constraint.
        first = _seed_proposal(session, canonical="苏州", vocab_type="region", proposal_key="k1")
        second = _seed_proposal(
            session, canonical="苏州", vocab_type="region", proposal_key="k2"
        )

        client = TestClient(create_app())
        ok = client.post(
            f"/v1/vocabulary/proposals/{first.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        assert ok.status_code == 200
        dup = client.post(
            f"/v1/vocabulary/proposals/{second.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        assert dup.status_code == 409
        assert "already exists" in dup.json()["detail"]

    def test_decide_already_decided_returns_409(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Twice Vocab", "twice-vocab")
        operator = _make_operator(session, tenant_id)
        proposal = _seed_proposal(session)

        client = TestClient(create_app())
        headers = _headers(tenant_id, operator)
        assert (
            client.post(
                f"/v1/vocabulary/proposals/{proposal.id}/decide",
                json={"decision": "REJECTED", "decided_by": "operator"},
                headers=headers,
            ).status_code
            == 200
        )
        second = client.post(
            f"/v1/vocabulary/proposals/{proposal.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=headers,
        )
        assert second.status_code == 409

    def test_decide_missing_proposal_returns_404(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Missing Vocab", "missing-vocab")
        operator = _make_operator(session, tenant_id)
        client = TestClient(create_app())
        response = client.post(
            "/v1/vocabulary/proposals/nope/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        assert response.status_code == 404

    def test_decide_requires_vocabulary_write_scope(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Readonly Vocab", "readonly-vocab")
        reader = _make_readonly_user(session, tenant_id)
        proposal = _seed_proposal(session)

        client = TestClient(create_app())
        response = client.post(
            f"/v1/vocabulary/proposals/{proposal.id}/decide",
            json={"decision": "APPROVED", "decided_by": "reader"},
            headers=_headers(tenant_id, reader),
        )
        assert response.status_code == 403


class TestList:
    def test_list_proposals_returns_pending_only(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("List Vocab", "list-vocab")
        operator = _make_operator(session, tenant_id)
        pending = _seed_proposal(session, canonical="榴莲")
        _seed_proposal(session, canonical="苏州", vocab_type="region", proposal_key="k-suzhou")

        client = TestClient(create_app())
        response = client.get(
            "/v1/vocabulary/proposals", headers=_headers(tenant_id, operator)
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert {i["canonical"] for i in items} == {"榴莲", "苏州"}
        # Newest first: 苏州 (seeded later) sorts ahead.
        assert items[0]["canonical"] == "苏州"

        # After deciding one, the default PENDING view drops it.
        client.post(
            f"/v1/vocabulary/proposals/{pending.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        response = client.get(
            "/v1/vocabulary/proposals", headers=_headers(tenant_id, operator)
        )
        assert [i["canonical"] for i in response.json()["items"]] == ["苏州"]

    def test_list_terms_returns_approved_terms(self) -> None:
        from apps.api.main import create_app

        session = get_session()
        tenant_id = _create_tenant("Terms Vocab", "terms-vocab")
        operator = _make_operator(session, tenant_id)
        proposal = _seed_proposal(session, canonical="榴莲")

        client = TestClient(create_app())
        client.post(
            f"/v1/vocabulary/proposals/{proposal.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        response = client.get(
            "/v1/vocabulary/terms", headers=_headers(tenant_id, operator)
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(
            item["canonical"] == "榴莲" and item["vocab_type"] == "product"
            for item in items
        )


class TestBatchTask:
    def test_batch_task_with_stub_llm_creates_proposals_and_marks_processed(
        self, monkeypatch
    ) -> None:
        from apps.worker.vocabulary_tasks import extract_vocabulary_proposals

        session = get_session()
        tenant_id = _create_tenant("Batch Vocab", "batch-vocab")
        session.add(
            VocabularyObservation(
                run_id="batch-r1", tenant_id=tenant_id, query="榴莲多少钱", tier="tier2"
            )
        )
        session.commit()

        monkeypatch.setenv("VOCABULARY_LLM_UPDATES_ENABLED", "true")

        def stub_llm_complete(prompt: str) -> str:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "canonical": "榴莲",
                            "type": "product",
                            "confidence": 0.9,
                            "evidence": ["榴莲多少钱"],
                        }
                    ]
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(
            "apps.worker.vocabulary_tasks.build_real_llm_complete",
            lambda settings: stub_llm_complete,
        )

        result = extract_vocabulary_proposals.delay().get()
        assert result["status"] == "ok"
        assert result["captured"] == 1
        assert result["proposals"] == 1

        observation = (
            session.query(VocabularyObservation).filter_by(run_id="batch-r1").first()
        )
        assert observation is not None
        assert observation.processed is True
        proposal = (
            session.query(VocabularyProposal).filter_by(canonical="榴莲").first()
        )
        assert proposal is not None
        assert proposal.status == "PENDING"

        # Flag off -> the task exits immediately without touching observations.
        monkeypatch.setenv("VOCABULARY_LLM_UPDATES_ENABLED", "false")
        session.add(
            VocabularyObservation(
                run_id="batch-r2", tenant_id=tenant_id, query="榴莲多少钱", tier="tier2"
            )
        )
        session.commit()
        skipped = extract_vocabulary_proposals.delay().get()
        assert skipped["status"] == "skipped"
        assert (
            session.query(VocabularyObservation).filter_by(run_id="batch-r2").first()
        ).processed is False


class TestRuntimeEffect:
    def test_approved_region_routes_tier1_after_approval(self, monkeypatch) -> None:
        """The decision invalidates the loader cache; the next route_query_layer
        rebuilds from the DB and the newly-approved region drops out of the
        uncovered set — the OOV supplier query now routes tier1."""
        from apps.api.main import create_app
        from erp_copilot.agent.routing import route_query_layer

        monkeypatch.setenv("VOCABULARY_LLM_UPDATES_ENABLED", "true")
        loader.invalidate()
        # 武汉 is an uncovered city (not in the manifest seed): the supplier
        # query routes up to tier2 rather than overgrabbing getSupplierByStatus.
        assert route_query_layer("武汉有哪些供应商") == "tier2"

        session = get_session()
        tenant_id = _create_tenant("Runtime Vocab", "runtime-vocab")
        operator = _make_operator(session, tenant_id)
        proposal = _seed_proposal(
            session,
            canonical="武汉",
            vocab_type="region",
            proposal_key="key-wuhan",
        )
        client = TestClient(create_app())
        response = client.post(
            f"/v1/vocabulary/proposals/{proposal.id}/decide",
            json={"decision": "APPROVED", "decided_by": "operator"},
            headers=_headers(tenant_id, operator),
        )
        assert response.status_code == 200
        assert route_query_layer("武汉有哪些供应商") == "tier1"
