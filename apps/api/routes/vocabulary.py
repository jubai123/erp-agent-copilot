"""Vocabulary pipeline endpoints — LLM-proposed term approval (Phase B).

GET /v1/vocabulary/proposals lists proposals awaiting an operator; POST
/v1/vocabulary/proposals/{proposal_id}/decide approves or rejects one — an
approved decision writes a vocabulary_terms row and invalidates the loader
cache so the API process reflects the change immediately; GET
/v1/vocabulary/terms lists the approved runtime terms. List and terms need
only an authenticated actor; decide additionally requires the vocabulary:write
scope it exercises.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.api.schemas.vocabulary import (
    DecideProposalRequest,
    DecideProposalResponse,
    ProposalItem,
    ProposalListResponse,
    TermItem,
    TermListResponse,
)
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.infrastructure.database import get_session
from erp_copilot.security.dependencies import Actor, get_actor, require_scope
from erp_copilot.vocabulary.service import VocabularyProposalService

router = APIRouter(prefix="/v1/vocabulary", tags=["vocabulary"])


@router.get("/proposals", response_model=ProposalListResponse)
def list_proposals(
    status: str = "PENDING",
    actor: Actor = Depends(get_actor),
) -> ProposalListResponse:
    """Return vocabulary proposals awaiting a decision (newest first)."""
    session = get_session()
    try:
        proposals = VocabularyProposalService(session).list_proposals(status=status)
        return ProposalListResponse(
            items=[
                ProposalItem(
                    id=proposal.id,
                    vocab_type=proposal.vocab_type,
                    canonical=proposal.canonical,
                    aliases=json.loads(proposal.aliases or "[]"),
                    confidence=proposal.confidence,
                    status=proposal.status,
                    created_at=proposal.created_at,
                )
                for proposal in proposals
            ]
        )
    finally:
        session.close()


@router.post("/proposals/{proposal_id}/decide", response_model=DecideProposalResponse)
def decide_proposal(
    proposal_id: str,
    body: DecideProposalRequest,
    request: Request,
    actor: Actor = Depends(require_scope("vocabulary:write")),
) -> DecideProposalResponse:
    """Approve or reject one PENDING vocabulary proposal.

    Approval writes a vocabulary_terms row (deduped by the database) and
    invalidates the loader cache; the API process merges the new term into the
    entity catalogs on the next get_catalog(). A decided proposal is never
    re-decided, and approving a term that already exists returns 409.
    """
    session = get_session()
    try:
        proposal = VocabularyProposalService(session).decide(
            proposal_id=proposal_id,
            decision=body.decision,
            decided_by=body.decided_by,
            reason=body.reason or "",
            ip=request.client.host if request.client else None,
            trace_id=body.trace_id,
        )
        return DecideProposalResponse(
            proposal_id=proposal.id,
            decision=proposal.status,
            vocab_type=proposal.vocab_type,
            canonical=proposal.canonical,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except CopilotError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    finally:
        session.close()


@router.get("/terms", response_model=TermListResponse)
def list_terms(
    vocab_type: str | None = None,
    actor: Actor = Depends(get_actor),
) -> TermListResponse:
    """Return active runtime vocabulary terms (manifest seed + approved)."""
    session = get_session()
    try:
        terms = VocabularyProposalService(session).list_terms(vocab_type=vocab_type)
        return TermListResponse(
            items=[
                TermItem(
                    id=term.id,
                    vocab_type=term.vocab_type,
                    canonical=term.canonical,
                    aliases=json.loads(term.aliases or "[]"),
                    source=term.source,
                    created_at=term.created_at,
                )
                for term in terms
            ]
        )
    finally:
        session.close()
