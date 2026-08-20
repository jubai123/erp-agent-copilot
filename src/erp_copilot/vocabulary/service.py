"""Vocabulary proposal service — human approval of LLM-proposed terms.

Mirrors the FailureQueue service pattern: session injected, commit inside the
service. decide() flips a PENDING proposal to APPROVED/REJECTED; an APPROVED
decision writes a VocabularyTerm that the runtime loader merges into the entity
catalogs. Every decision appends an immutable audit_logs row and invalidates
the loader cache so the API process reflects the change immediately.

LLM output is never authoritative: nothing reaches vocabulary_terms without
this human gate. The (vocab_type, canonical) unique constraint backs the
dedup — approving a term that already exists collides at the database and
surfaces as a 409 CopilotError instead of a silent duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import AuditLog, VocabularyProposal, VocabularyTerm
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.vocabulary.loader import invalidate

PROPOSAL_ALREADY_DECIDED = "PROPOSAL_ALREADY_DECIDED"
PROPOSAL_DUPLICATE_TERM = "PROPOSAL_DUPLICATE_TERM"

Decision = Literal["APPROVED", "REJECTED"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class VocabularyProposalService:
    """List, approve and reject vocabulary proposals within a tenant boundary."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_proposals(
        self, *, status: str = "PENDING", limit: int = 50
    ) -> list[VocabularyProposal]:
        """Return proposals by status, newest first (global across tenants, v1)."""
        return (
            self._session.query(VocabularyProposal)
            .filter_by(status=status)
            .order_by(VocabularyProposal.created_at.desc(), VocabularyProposal.id.desc())
            .limit(limit)
            .all()
        )

    def list_terms(
        self, *, vocab_type: str | None = None, limit: int = 100
    ) -> list[VocabularyTerm]:
        """Return active runtime terms, newest first; optionally by type."""
        query = self._session.query(VocabularyTerm).filter_by(is_active=True)
        if vocab_type is not None:
            query = query.filter_by(vocab_type=vocab_type)
        return query.order_by(VocabularyTerm.created_at.desc()).limit(limit).all()

    def decide(
        self,
        *,
        proposal_id: str,
        decision: Decision,
        decided_by: str,
        reason: str = "",
        ip: str | None = None,
        trace_id: str | None = None,
    ) -> VocabularyProposal:
        """Decide one PENDING proposal, atomically.

        Approve writes the VocabularyTerm (deduped by the database), flips the
        proposal, appends the audit row and invalidates the loader cache — all
        in one transaction, so a duplicate term rolls the whole decision back
        to a 409 and the proposal stays PENDING. A decided proposal is never
        re-decided (409 PROPOSAL_ALREADY_DECIDED).
        """
        proposal = self._session.query(VocabularyProposal).filter_by(id=proposal_id).first()
        if proposal is None:
            raise NotFoundError(f"Proposal '{proposal_id}' not found")
        if proposal.status != "PENDING":
            raise CopilotError(
                f"Proposal '{proposal_id}' is already {proposal.status}",
                code=PROPOSAL_ALREADY_DECIDED,
            )

        if decision == "APPROVED":
            self._session.add(
                VocabularyTerm(
                    vocab_type=proposal.vocab_type,
                    canonical=proposal.canonical,
                    aliases=proposal.aliases,
                    source="approved_proposal",
                )
            )
        proposal.status = decision
        proposal.decided_by = decided_by
        proposal.decided_at = _utcnow()
        proposal.decision_reason = reason or None
        self._session.add(
            AuditLog(
                actor=decided_by,
                resource=f"vocabulary_proposal:{proposal.id}",
                action="approve" if decision == "APPROVED" else "reject",
                result=decision,
                ip=ip,
                trace_id=trace_id,
            )
        )
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise CopilotError(
                f"Term '{proposal.canonical}' already exists for type '{proposal.vocab_type}'",
                code=PROPOSAL_DUPLICATE_TERM,
            ) from exc
        invalidate()
        return proposal
