"""MCP Gateway — FastAPI entry point for persistent MCP server connections.

Provides HTTP endpoints to manage and execute tools through
connected MCP-compatible tool servers.

Egress control (docs/06 §6, task 5.3): every registered server URL is
validated by an :class:`SSRFGuard` before it enters the registry, and each
connection re-checks its target in :meth:`MCPGatewayConnection.connect` before
a session exists. A blocked URL raises 422 and is persisted to ``security_events``
through the gateway's own database session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import get_session, init_db
from erp_copilot.observability.http import TraceContextMiddleware
from erp_copilot.observability.logging import setup_logging
from erp_copilot.observability.tracing import build_otlp_exporter, setup_tracing
from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard, record_security_event
from erp_copilot.tools.mcp_gateway import MCPGateway, MCPGatewayConnection

_SERVICE_NAME = "mcp-gateway"


def _split_hosts(value: str) -> frozenset[str]:
    """Parse a comma-separated host list into a frozenset, ignoring blanks."""
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _build_ssrf_guard(settings: Settings) -> SSRFGuard:
    """Build the egress guard from Settings — fail-closed when nothing is allowed."""
    return SSRFGuard(
        SSRFConfig(
            allowed_schemes=frozenset({"https"}),
            allowed_hosts=_split_hosts(settings.mcp_egress_allowed_hosts),
            allowed_ports=frozenset({443}),
            trusted_internal_hosts=_split_hosts(settings.mcp_egress_trusted_internal_hosts),
        )
    )


class RegisterServerRequest(BaseModel):
    """Body for POST /servers — a named MCP server connection to register."""

    name: str
    url: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize DB + observability at startup (uvicorn fires this).

    ``create_engine`` is lazy, so a briefly-down database does not block
    startup; the first query surfaces the connection failure. TestClient does
    not fire lifespan events, so integration tests inject the guard (and rely
    on their own conftest) instead.
    """
    settings = Settings()  # type: ignore[call-arg]
    init_db(settings)
    setup_logging(level=settings.log_level, service=_SERVICE_NAME)
    setup_tracing(
        service_name=_SERVICE_NAME,
        exporter=build_otlp_exporter(settings.otel_exporter_endpoint),
    )
    app.state.ssrf_guard = _build_ssrf_guard(settings)
    yield


def create_gateway_app(guard: SSRFGuard | None = None) -> FastAPI:
    """Create a FastAPI application for the MCP Gateway.

    The returned app exposes lifecycle hooks and endpoints for
    registering, listing, and executing tools through MCP connections.
    ``guard`` is injected for tests; in production it is built from Settings
    in the lifespan (defaulting to fail-closed: no egress until configured).
    """
    app = FastAPI(
        title="ERP Agent Copilot - MCP Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    app.state.ssrf_guard = guard
    gateway = MCPGateway()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "mcp-gateway"}

    @app.get("/servers")
    async def list_servers() -> dict[str, list[str]]:
        return {"servers": gateway.list_servers()}

    @app.post("/servers", status_code=201)
    async def register_server(body: RegisterServerRequest) -> dict[str, str]:
        """Register a named MCP server connection, enforcing the egress policy.

        A URL that fails :class:`SSRFGuard` is rejected with 422 and the block
        is recorded to ``security_events`` (record commits before the raise, so
        the audit row survives the error path). Registered connections carry the
        same guard, so a later ``connect()`` re-checks the target (fail-closed).
        """
        ssrf_guard = app.state.ssrf_guard
        if ssrf_guard is None:
            raise HTTPException(status_code=500, detail="SSRF guard not configured")
        verdict = ssrf_guard.check(body.url)
        if not verdict.allowed:
            record_security_event(get_session(), run_id=None, url=body.url, verdict=verdict)
            raise HTTPException(
                status_code=422,
                detail=f"URL blocked by egress policy: {verdict.reason} ({verdict.detail})",
            )
        gateway.register(body.name, MCPGatewayConnection(body.url, guard=ssrf_guard))
        return {"name": body.name, "url": body.url, "status": "registered"}

    return app
