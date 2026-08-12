"""Pydantic schemas for Run creation, query, and approval."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CreateRunRequest(BaseModel):
    tenant_id: str
    title: str = ""
    product_name: str = "苹果"
    # Natural-language query the planner operates on. Without it the worker
    # falls back to "查询{product_name}库存", which can only ever produce READ
    # plans — a WRITE query ("下一单…") must be passed here to reach approval.
    query: str | None = None


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
