"""Prometheus metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from erp_copilot.observability.metrics import generate_latest

router = APIRouter(tags=["metrics"])

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Expose platform metrics in the Prometheus text exposition format."""
    return Response(content=generate_latest(), headers={"Content-Type": _CONTENT_TYPE})
