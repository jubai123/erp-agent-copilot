"""MCP Gateway — FastAPI entry point for persistent MCP server connections.

Provides HTTP endpoints to manage and execute tools through
connected MCP-compatible tool servers.
"""

from __future__ import annotations

from fastapi import FastAPI

from erp_copilot.tools.mcp_gateway import MCPGateway


def create_gateway_app() -> FastAPI:
    """Create a FastAPI application for the MCP Gateway.

    The returned app exposes lifecycle hooks and endpoints for
    registering, listing, and executing tools through MCP connections.
    """
    app = FastAPI(title="ERP Agent Copilot - MCP Gateway", version="0.1.0")
    gateway = MCPGateway()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "mcp-gateway"}

    @app.get("/servers")
    async def list_servers() -> dict[str, list[str]]:
        return {"servers": gateway.list_servers()}

    return app
