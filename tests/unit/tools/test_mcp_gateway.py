"""Tests for MCP Gateway — persistent connection management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


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
            result = asyncio.run(
                conn.execute_tool("create_order", {"product_id": 1})
            )

            assert result == {"result": "ok"}
            mock_session.call_tool.assert_awaited_once_with(
                "create_order", {"product_id": 1}
            )

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
