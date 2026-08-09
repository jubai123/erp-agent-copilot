"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    """Return application health status."""
    return {
        "status": "ok",
        "app_name": "erp-agent-copilot",
        "version": "0.1.0",
    }
