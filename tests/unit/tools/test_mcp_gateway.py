"""Tests for MCP Gateway — persistent connection management."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, Mock, patch

import pytest

from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard


class TestMCPGatewayConnection:
    """Acceptance: Single connection wraps a ClientSession with lifecycle."""

    def test_initial_state_is_disconnected(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        conn = MCPGatewayConnection("http://localhost:8000/mcp")
        assert conn.is_connected is False
        assert conn.url == "http://localhost:8000/mcp"

    def test_connect_establishes_session(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_session,
        ):
            conn = MCPGatewayConnection("http://localhost:8000/mcp")

            import asyncio

            asyncio.run(conn.connect())

        assert conn.is_connected is True
        mock_session.initialize.assert_awaited_once()

    def test_disconnect_cleans_up(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_session,
        ):
            conn = MCPGatewayConnection("http://localhost:8000/mcp")

            import asyncio

            asyncio.run(conn.connect())
            assert conn.is_connected is True

            asyncio.run(conn.disconnect())
            assert conn.is_connected is False

    def test_second_connect_is_noop(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_session,
        ):
            conn = MCPGatewayConnection("http://localhost:8000/mcp")

            import asyncio

            asyncio.run(conn.connect())
            call_count = mock_session.initialize.call_count
            asyncio.run(conn.connect())  # Second connect should be no-op

            assert mock_session.initialize.call_count == call_count

    def test_execute_delegates_to_connected_session(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value={"result": "ok"})

        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_session,
        ):
            conn = MCPGatewayConnection("http://localhost:8000/mcp")

            import asyncio

            asyncio.run(conn.connect())
            result = asyncio.run(conn.execute_tool("create_order", {"product_id": 1}))

            assert result == {"result": "ok"}
            mock_session.call_tool.assert_awaited_once_with("create_order", {"product_id": 1})

    def test_execute_without_connect_raises(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        conn = MCPGatewayConnection("http://localhost:8000/mcp")

        import asyncio

        with pytest.raises(RuntimeError, match="not connected"):
            asyncio.run(conn.execute_tool("test", {}))


class TestMCPGateway:
    """Acceptance: Gateway manages multiple named server connections."""

    def test_register_and_get_connection(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGateway, MCPGatewayConnection

        gateway = MCPGateway()

        conn = MCPGatewayConnection("http://localhost:8000/mcp")
        gateway.register("erp", conn)

        assert gateway.get("erp") is conn

    def test_get_unregistered_returns_none(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGateway

        gateway = MCPGateway()
        assert gateway.get("nonexistent") is None

    def test_unregister_removes_connection(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGateway, MCPGatewayConnection

        gateway = MCPGateway()
        conn = MCPGatewayConnection("http://localhost:8000/mcp")
        gateway.register("erp", conn)

        gateway.unregister("erp")
        assert gateway.get("erp") is None

    def test_list_registered_servers(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGateway, MCPGatewayConnection

        gateway = MCPGateway()
        gateway.register("erp", MCPGatewayConnection("http://localhost:8000/mcp"))
        gateway.register("crm", MCPGatewayConnection("http://localhost:9000/mcp"))

        servers = gateway.list_servers()
        assert servers == ["erp", "crm"]


def _fake_resolver(table: dict[str, list[str]]) -> Callable[[str], list[str]]:
    """Deterministic stand-in for socket.getaddrinfo (offline unit test)."""
    return lambda host: list(table.get(host, []))


def _guard(
    *,
    allowed_hosts: frozenset[str] = frozenset({"api.erp.example.com"}),
    dns: dict[str, list[str]] | None = None,
) -> SSRFGuard:
    """Guard with a fake resolver so no test touches real DNS."""
    return SSRFGuard(
        SSRFConfig(
            allowed_schemes=frozenset({"https"}),
            allowed_hosts=allowed_hosts,
            allowed_ports=frozenset({443}),
        ),
        resolve=_fake_resolver(dns or {"api.erp.example.com": ["93.184.216.34"]}),
    )


class TestMCPGatewayConnectionSSRF:
    """Acceptance: a guard on the connection gates egress before a session exists."""

    def test_connect_allows_allowlisted_public_url(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_session,
        ):
            conn = MCPGatewayConnection("https://api.erp.example.com", guard=_guard())

            import asyncio

            asyncio.run(conn.connect())

        assert conn.is_connected is True
        mock_session.initialize.assert_awaited_once()

    def test_connect_blocks_dns_rebinding_url_and_never_builds_session(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection, SSRFBlockedError

        # partner.erp.example.com is allowlisted but resolves to an RFC1918
        # address — a DNS rebinding attack against the internal network.
        guard = _guard(
            allowed_hosts=frozenset({"partner.erp.example.com"}),
            dns={"partner.erp.example.com": ["10.0.0.5"]},
        )
        mock_client_session = Mock()
        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_client_session,
        ):
            conn = MCPGatewayConnection("https://partner.erp.example.com", guard=guard)

            import asyncio

            with pytest.raises(SSRFBlockedError) as exc_info:
                asyncio.run(conn.connect())

        assert exc_info.value.reason == "BLOCKED_IP"
        assert exc_info.value.url == "https://partner.erp.example.com"
        assert conn.is_connected is False
        mock_client_session.assert_not_called()

    def test_connect_blocks_host_not_in_allowlist(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection, SSRFBlockedError

        conn = MCPGatewayConnection("https://evil.example.com", guard=_guard())

        import asyncio

        with pytest.raises(SSRFBlockedError) as exc_info:
            asyncio.run(conn.connect())

        assert exc_info.value.reason == "HOST_NOT_ALLOWED"

    def test_connect_without_guard_behaves_unchanged(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        with patch(
            "erp_copilot.tools.mcp_gateway.ClientSession",
            return_value=mock_session,
        ):
            conn = MCPGatewayConnection("https://api.erp.example.com")

            import asyncio

            asyncio.run(conn.connect())

        assert conn.is_connected is True
        mock_session.initialize.assert_awaited_once()
