"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import get_session, init_db
from erp_copilot.observability.http import TraceContextMiddleware
from erp_copilot.observability.logging import setup_logging
from erp_copilot.observability.tracing import build_otlp_exporter, setup_tracing
from erp_copilot.tools.contract import ensure_contracts_loaded

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize DB engine and observability at startup (uvicorn fires this).

    ``create_engine`` is lazy, so a briefly-down database does not block
    startup; the first query surfaces the connection failure. TestClient does
    not fire lifespan events, so the unit/integration suites initialize the
    engine through their own conftest fixtures.
    """
    settings = Settings()  # type: ignore[call-arg]
    init_db(settings)
    setup_logging(level=settings.log_level, service=settings.app_name)
    seeded = ensure_contracts_loaded(get_session())
    _logger.info("seeded %d tool contracts from the registry authority", seeded)
    setup_tracing(
        service_name=settings.app_name,
        exporter=build_otlp_exporter(settings.otel_exporter_endpoint),
    )
    yield


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="ERP Agent Copilot",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)

    from apps.api.routes.api_keys import router as api_keys_router
    from apps.api.routes.failures import router as failures_router
    from apps.api.routes.health import router as health_router
    from apps.api.routes.knowledge import router as knowledge_router
    from apps.api.routes.metrics import router as metrics_router
    from apps.api.routes.runs import router as runs_router
    from apps.api.routes.tools import router as tools_router
    from apps.api.routes.vocabulary import router as vocabulary_router

    app.include_router(health_router)
    app.include_router(tools_router)
    app.include_router(runs_router)
    app.include_router(failures_router)
    app.include_router(knowledge_router)
    app.include_router(metrics_router)
    app.include_router(api_keys_router)
    app.include_router(vocabulary_router)

    return app
