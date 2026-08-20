"""Integration tests for the vocabulary pipeline tables (Phase B schema).

Base.metadata.create_all (tests/integration/conftest.py) and the Alembic
migration f2c3d4e5f6a7 both create these tables; the models must accept inserts
and enforce the unique constraints the approval flow relies on (a duplicate
approve of the same canonical term must collide at the database -> 409).

Tests run against the dedicated test database erp_copilot_test.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from erp_copilot.domain.entities import (
    VocabularyObservation,
    VocabularyProposal,
    VocabularyTerm,
)
from erp_copilot.infrastructure.database import get_session


class TestVocabularyTerms:
    def test_insert_and_readback(self) -> None:
        session = get_session()
        try:
            term = VocabularyTerm(
                vocab_type="region",
                canonical="苏州",
                aliases='["苏州工业园区"]',
            )
            session.add(term)
            session.commit()
            session.refresh(term)
            assert term.id
            assert term.is_active is True
            assert term.source == "approved_proposal"
        finally:
            session.close()

    def test_unique_constraint_rejects_duplicate_term(self) -> None:
        session = get_session()
        try:
            session.add(VocabularyTerm(vocab_type="product", canonical="榴莲"))
            session.commit()
            session.add(VocabularyTerm(vocab_type="product", canonical="榴莲"))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.close()


class TestVocabularyObservations:
    def test_unique_run_tenant(self) -> None:
        # tenant_id is part of the key; a NULL would let Postgres treat the two
        # rows as distinct (NULLs are never equal), so use a concrete tenant —
        # runs always carry one.
        session = get_session()
        try:
            session.add(
                VocabularyObservation(
                    run_id="r1", tenant_id="t1", query="榴莲多少钱", tier="tier2"
                )
            )
            session.commit()
            session.add(
                VocabularyObservation(
                    run_id="r1", tenant_id="t1", query="榴莲多少钱", tier="tier2"
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.close()


class TestVocabularyProposals:
    def test_insert_and_readback(self) -> None:
        session = get_session()
        try:
            proposal = VocabularyProposal(
                proposal_key="sha1-product-榴莲",
                vocab_type="product",
                canonical="榴莲",
                aliases="[]",
                evidence='[{"query": "榴莲多少钱"}]',
                confidence=0.9,
            )
            session.add(proposal)
            session.commit()
            session.refresh(proposal)
            assert proposal.status == "PENDING"
        finally:
            session.close()

    def test_unique_proposal_key(self) -> None:
        session = get_session()
        try:
            session.add(
                VocabularyProposal(
                    proposal_key="k", vocab_type="product", canonical="榴莲"
                )
            )
            session.commit()
            session.add(
                VocabularyProposal(
                    proposal_key="k", vocab_type="region", canonical="苏州"
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.close()
