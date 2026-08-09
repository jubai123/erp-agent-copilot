"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="ERP Agent Copilot",
        version="0.1.0",
    )

    from apps.api.routes.health import router as health_router
    from apps.api.routes.knowledge import router as knowledge_router
    from apps.api.routes.metrics import router as metrics_router
    from apps.api.routes.runs import router as runs_router
    from apps.api.routes.tools import router as tools_router

    app.include_router(health_router)
    app.include_router(tools_router)
    app.include_router(runs_router)
    app.include_router(knowledge_router)
    app.include_router(metrics_router)

    return app
