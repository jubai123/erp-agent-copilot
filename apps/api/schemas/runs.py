"""Pydantic schemas for Run creation, query, and approval."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CreateRunRequest(BaseModel):
    tenant_id: str
    title: str = ""
    product_name: str = "苹果"


class RunResponse(BaseModel):
    run_id: str
    tenant_id: str
    title: str
    status: str
    created_at: datetime | None = None


class ApproveRunRequest(BaseModel):
    step_id: str
    decision: Literal["APPROVE", "DENY"]
    decided_by: str
    reason: str | None = None
    trace_id: str | None = None
