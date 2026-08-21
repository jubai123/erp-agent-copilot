"""E2E: MCPGatewayConnection drives a real Streamable HTTP MCP server.

Serves apps.mcp_gateway.demo_server (an MCP SDK 2.0 MCPServer) under uvicorn on
a free port, then drives it with :class:`MCPGatewayConnection` over the real
transport — connect -> list_tools -> execute_tool -> disconnect — with no mocks
on the wire. The client's initialize handshake and tool calls go over HTTP to
the running server, which is the artifact that retires "MCP 是展示件"
(the pre-fix ClientSession had no transport and could never connect).

The SSRF guard is also exercised against the reachable server: a URL the guard
rejects raises SSRFBlockedError before any session exists, proving the egress
policy gates egress even when the target is up.

These tests need no database and no MCP mocks; the only moving parts are the
uvicorn thread (localhost) and the real SDK client.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn

from apps.mcp_gateway.demo_server import build_demo_app
from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard
from erp_copilot.tools.mcp_gateway import MCPGatewayConnection, SSRFBlockedError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(base_url: str) -> None:
    """Poll the demo server's /health route until it answers 200.

    trust_env=False: the dev machine's system proxy (Clash on 127.0.0.1:7890)
    otherwise intercepts the loopback request and returns 502 — the same proxy
    the MCP client already bypasses (trust_env=False on its httpx2 client), so
    the probe must share that egress posture to reach the same target.
    """
    last_error: Exception | None = None
    with httpx.Client(trust_env=False) as client:
        for _ in range(100):
            try:
                if client.get(f"{base_url}/health", timeout=1).status_code == 200:
                    return
            except Exception as exc:  # pragma: no cover - transient until boot
                last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"demo MCP server did not become ready: {last_error!r}")


@pytest.fixture()
def demo_mcp_url() -> Iterator[str]:
    """Serve the demo MCP server on a free port; yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(build_demo_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base_url)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _localhost_guard(port: int) -> SSRFGuard:
    """Guard that admits http://127.0.0.1:<port> — the demo server's URL.

    127.0.0.1 is loopback, so it must also be in trusted_internal_hosts or the
    address check returns BLOCKED_IP (the fail-closed default for non-public
    addresses). This mirrors the production gateway's
    mcp_egress_trusted_internal_hosts knob for the ERP simulator.
    """
    return SSRFGuard(
        SSRFConfig(
            allowed_schemes=frozenset({"http"}),
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({port}),
            trusted_internal_hosts=frozenset({"127.0.0.1"}),
        )
    )


def _run_in_one_loop(coro) -> Any:
    """Drive the whole connection lifecycle in a single event loop.

    The MCP client's anyio task group is loop-bound, so connect() and
    disconnect() must run in the same asyncio.run; a fresh loop per call would
    break teardown.
    """
    return asyncio.run(coro)


class TestRealTransportRoundTrip:
    """Acceptance: a real MCP server is initialized, listed and executed against."""

    def test_connect_list_tools_execute_disconnect(self, demo_mcp_url: str) -> None:
        port = int(demo_mcp_url.rsplit(":", 1)[1])
        url = f"{demo_mcp_url}/mcp"

        async def exercise() -> tuple[
            list[str], dict[str, Any], dict[str, Any], MCPGatewayConnection
        ]:
            conn = MCPGatewayConnection(url, guard=_localhost_guard(port))
            await conn.connect()
            try:
                tools = await conn.list_tools()
                product = await conn.execute_tool("getProductByName", {"name": "苹果"})
                suppliers = await conn.execute_tool(
                    "querySuppliersByDeliveryRegion", {"region": "上海"}
                )
                available = await conn.execute_tool("getSupplierByStatus", {"status": "AVAILABLE"})
                return (
                    tools,
                    product.structured_content,
                    suppliers.structured_content,
                    available.structured_content,
                    conn,
                )
            finally:
                await conn.disconnect()

        tools, product, suppliers, available, conn = _run_in_one_loop(exercise())

        # Torn down inside the loop: the session and transport are closed.
        assert conn.is_connected is False
        assert tools == [
            "getProductByName",
            "getProductById",
            "getProductSubstitutes",
            "getProductSubstitutesByName",
            "getBatchProductByProductIds",
            "getSupplierByStatus",
            "querySuppliersByDeliveryRegion",
            "getSupplierByName",
            "getSupplierById",
            "createOrder",
            "getOrderByOrderId",
        ]
        assert product["product_id"] == 1
        assert product["name"] == "苹果"
        assert product["stock"] == 100
        # 上海 is covered by 华东物流 (supplier_id=3) in the seed data.
        assert [s["supplier_id"] for s in suppliers["suppliers"]] == [3]
        assert suppliers["supplier_id"] == 3
        # 华东物流 (supplier_id=3) is the first AVAILABLE supplier.
        assert available["supplier_id"] == 3

    def test_unknown_product_surfaces_is_error(self, demo_mcp_url: str) -> None:
        port = int(demo_mcp_url.rsplit(":", 1)[1])
        url = f"{demo_mcp_url}/mcp"

        async def exercise() -> Any:
            conn = MCPGatewayConnection(url, guard=_localhost_guard(port))
            await conn.connect()
            try:
                return await conn.execute_tool("getProductByName", {"name": "不存在"})
            finally:
                await conn.disconnect()

        result = _run_in_one_loop(exercise())

        # The server raises ValueError for a missing product; the SDK turns it
        # into CallToolResult(is_error=True) — the executor analogue of
        # ToolResult.failure(PRODUCT_NOT_FOUND), not a crash.
        assert result.is_error is True
        assert "not found" in result.content[0].text


class TestGuardAgainstReachableServer:
    """Acceptance: the SSRF guard gates egress even when the target is up."""

    def test_guard_blocks_before_any_session(self, demo_mcp_url: str) -> None:
        port = int(demo_mcp_url.rsplit(":", 1)[1])
        url = f"{demo_mcp_url}/mcp"

        # The server is reachable, but the policy admits a different port — the
        # check must fail closed before any egress, so no session is ever built.
        guard = SSRFGuard(
            SSRFConfig(
                allowed_schemes=frozenset({"http"}),
                allowed_hosts=frozenset({"127.0.0.1"}),
                allowed_ports=frozenset({port + 1}),
                trusted_internal_hosts=frozenset({"127.0.0.1"}),
            )
        )
        conn = MCPGatewayConnection(url, guard=guard)

        with pytest.raises(SSRFBlockedError) as exc_info:
            _run_in_one_loop(conn.connect())

        assert exc_info.value.reason == "BLOCKED_PORT"
        assert conn.is_connected is False
