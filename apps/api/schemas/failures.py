"""Pydantic schemas for the human-intervention failure queue (task 5.9)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from erp_copilot.application.failure_queue import ResolveDecision


class FailureItem(BaseModel):
    """One run waiting for human intervention, with its diagnostic detail."""

    run_id: str
    tenant_id: str
    title: str
    failure_code: str | None = None
    failure_reason: str | None = None
    suggested_action: str | None = None
    failed_at: datetime | None = None


class FailureListResponse(BaseModel):
    items: list[FailureItem]


class ResolveRunRequest(BaseModel):
    """A human's decision on one ambiguous write step (docs/06 §8)."""

    step_id: str
    decision: ResolveDecision
    decided_by: str
    reason: str | None = None


class ResolveRunResponse(BaseModel):
    run_id: str
    step_id: str
    decision: str
    decided_by: str
    status: str
