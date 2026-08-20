"""Pydantic schemas for the vocabulary pipeline API (Phase B)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProposalItem(BaseModel):
    """One LLM-proposed term awaiting a human decision."""

    id: str
    vocab_type: str
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    confidence: float
    status: str
    created_at: datetime


class ProposalListResponse(BaseModel):
    items: list[ProposalItem]


class DecideProposalRequest(BaseModel):
    """An operator's decision on one PENDING proposal."""

    decision: Literal["APPROVED", "REJECTED"]
    decided_by: str
    reason: str | None = None
    trace_id: str | None = None


class DecideProposalResponse(BaseModel):
    proposal_id: str
    decision: str
    vocab_type: str
    canonical: str


class TermItem(BaseModel):
    """One active runtime vocabulary term (manifest seed + approved terms)."""

    id: str
    vocab_type: str
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    source: str
    created_at: datetime


class TermListResponse(BaseModel):
    items: list[TermItem]
