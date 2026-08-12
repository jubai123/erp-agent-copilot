"""MCP Gateway — FastAPI entry point for persistent MCP server connections.

Provides HTTP endpoints to manage and execute tools through
connected MCP-compatible tool servers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from erp_copilot.infrastructure.config import Settings
from erp_copilot.observability.http import TraceContextMiddleware
from erp_copilot.observability.logging import setup_logging
from erp_copilot.observability.tracing import build_otlp_exporter, setup_tracing
from erp_copilot.tools.mcp_gateway import MCPGateway

_SERVICE_NAME = "mcp-gateway"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize observability at startup (uvicorn fires this).

    The gateway keeps its MCP connections in memory, so only JSON logging and
    the tracer provider are wired here, under this process's own
    ``service.name``.
    """
    settings = Settings()  # type: ignore[call-arg]
    setup_logging(level=settings.log_level, service=_SERVICE_NAME)
    setup_tracing(
        service_name=_SERVICE_NAME,
        exporter=build_otlp_exporter(settings.otel_exporter_endpoint),
    )
    yield


def create_gateway_app() -> FastAPI:
    """Create a FastAPI application for the MCP Gateway.

    The returned app exposes lifecycle hooks and endpoints for
    registering, listing, and executing tools through MCP connections.
    """
    app = FastAPI(
        title="ERP Agent Copilot - MCP Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    gateway = MCPGateway()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "mcp-gateway"}

    @app.get("/servers")
    async def list_servers() -> dict[str, list[str]]:
        return {"servers": gateway.list_servers()}

    return app
