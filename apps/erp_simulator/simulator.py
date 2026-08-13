"""ERP Simulator — FastAPI entry point.

Provides a standalone FastAPI application that simulates an ERP
backend with deterministic product, inventory, supplier, and order
endpoints. Written to avoid dependency on live external ERP systems.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from apps.erp_simulator.routes.orders import router as orders_router
from apps.erp_simulator.routes.products import router as products_router
from apps.erp_simulator.routes.suppliers import router as suppliers_router
from apps.erp_simulator.scenarios import router as scenarios_router
from apps.erp_simulator.security import require_simulator_token
from erp_copilot.infrastructure.config import Settings
from erp_copilot.observability.http import TraceContextMiddleware
from erp_copilot.observability.logging import setup_logging
from erp_copilot.observability.tracing import build_otlp_exporter, setup_tracing

_SERVICE_NAME = "erp-simulator"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize observability at startup (uvicorn fires this).

    The simulator is stateless (in-memory data), so unlike the API lifespan
    there is no DB engine to initialize; only JSON logging and the tracer
    provider are wired here, under this process's own ``service.name``.
    """
    settings = Settings()  # type: ignore[call-arg]
    setup_logging(level=settings.log_level, service=_SERVICE_NAME)
    setup_tracing(
        service_name=_SERVICE_NAME,
        exporter=build_otlp_exporter(settings.otel_exporter_endpoint),
    )
    yield


def create_simulator_app(*, simulator_shared_secret: str | None = None) -> FastAPI:
    """Create a FastAPI application for the ERP Simulator.

    ``simulator_shared_secret`` is the ``X-Simulator-Token`` shared secret for
    service-to-service auth. When it is ``None`` the value is read from
    ``Settings().simulator_shared_secret``; an empty secret disables the gate
    (open test double), a non-empty one fail-closes every route except
    ``/health``.
    """
    settings = Settings()  # type: ignore[call-arg]
    secret = (
        settings.simulator_shared_secret
        if simulator_shared_secret is None
        else simulator_shared_secret
    )
    app = FastAPI(
        title="ERP Agent Copilot - ERP Simulator",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)

    router_deps = [Depends(require_simulator_token(secret))] if secret else []
    app.include_router(products_router, dependencies=router_deps)
    app.include_router(suppliers_router, dependencies=router_deps)
    app.include_router(orders_router, dependencies=router_deps)
    app.include_router(scenarios_router, dependencies=router_deps)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "erp-simulator"}

    return app
