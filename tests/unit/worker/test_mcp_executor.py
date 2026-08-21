"""Unit tests for the worker's MCP executor adapter (apps/worker/mcp_executor.py).

The adapter normalizes an MCP server's CallToolResult into the internal
ToolResult shape behind the same async (tool_name, arguments) -> ToolResult
contract as the in-process simulator and the cloud HTTP executor. The
MCPGatewayConnection is mocked here (no sockets); the real-transport path is
covered by tests/integration/test_mcp_executor_worker.py. The executor is
async; tests drive it with asyncio.run.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from mcp.types import TextContent

from apps.worker.mcp_executor import MCPToolExecutor, build_mcp_executor
from erp_copilot.agent.retry_policy import AsyncRetryExecutor, RetryPolicy
from erp_copilot.security.ssrf_guard import SSRFGuard
from erp_copilot.tools.mcp_gateway import SSRFBlockedError
from erp_copilot.tools.tool_result import ToolResult


def _result(
    *,
    is_error: bool = False,
    structured_content: Any = None,
    text: str | None = None,
) -> Mock:
    res = Mock()
    res.is_error = is_error
    res.structured_content = structured_content
    res.content = [TextContent(text=text)] if text is not None else []
    return res


def _make_conn(*, connected: bool = False) -> Mock:
    """A Mock MCPGatewayConnection whose connect() flips is_connected."""
    conn = Mock()
    conn.is_connected = connected

    async def _connect() -> None:
        conn.is_connected = True

    conn.connect = AsyncMock(side_effect=_connect)
    conn.disconnect = AsyncMock()
    conn.execute_tool = AsyncMock(return_value=_result(structured_content={"ok": True}))
    return conn


def _build(conn: Mock) -> MCPToolExecutor:
    with patch("apps.worker.mcp_executor.MCPGatewayConnection", return_value=conn):
        return build_mcp_executor("http://localhost:8000/mcp")


def _call(
    executor: MCPToolExecutor, tool_name: str = "getProductByName", **arguments: Any
) -> ToolResult:
    return asyncio.run(executor(tool_name, arguments))


class TestSuccessMapping:
    def test_structured_content_dict_becomes_data(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(structured_content={"name": "苹果", "stock": 100})
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "SUCCEEDED"
        assert result.data == {"name": "苹果", "stock": 100}
        assert result.error is None

    def test_json_text_fallback_parsed_as_dict(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(text='{"a": 1}')
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "SUCCEEDED"
        assert result.data == {"a": 1}

    def test_non_json_text_wrapped_as_text(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(text="hello")
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "SUCCEEDED"
        assert result.data == {"text": "hello"}

    def test_empty_result_yields_empty_dict(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result()
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "SUCCEEDED"
        assert result.data == {}


class TestConnectionLifecycle:
    def test_auto_connects_on_first_call(self) -> None:
        conn = _make_conn()
        executor = _build(conn)

        result = _call(executor, name="苹果")

        assert result.status == "SUCCEEDED"
        conn.connect.assert_awaited_once()
        conn.execute_tool.assert_awaited_once_with("getProductByName", {"name": "苹果"})

    def test_reuses_connection_across_calls(self) -> None:
        conn = _make_conn()
        executor = _build(conn)

        _call(executor)
        _call(executor, tool_name="getSupplierByStatus", status="AVAILABLE")

        # connect flips is_connected on the first call, so the second reuses it.
        assert conn.connect.await_count == 1
        assert conn.execute_tool.await_count == 2

    def test_close_disconnects(self) -> None:
        conn = _make_conn()
        executor = _build(conn)
        _call(executor)

        asyncio.run(executor.close())

        conn.disconnect.assert_awaited_once()


class TestErrorMapping:
    def test_is_error_maps_to_permanent_failure(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(is_error=True, text="Product '不存在' not found")
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "MCP_TOOL_ERROR"
        assert result.error.error_message == "Product '不存在' not found"
        assert result.error.is_retryable is False

    def test_connect_failure_maps_retryable(self) -> None:
        conn = _make_conn()

        async def _boom() -> None:
            raise RuntimeError("connection refused")

        conn.connect = AsyncMock(side_effect=_boom)
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_UNAVAILABLE"
        assert result.error.is_retryable is True
        assert "connection refused" in result.error.error_message

    def test_execute_transport_error_maps_retryable(self) -> None:
        conn = _make_conn()

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("read timeout")

        conn.execute_tool = AsyncMock(side_effect=_boom)
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_UNAVAILABLE"
        assert result.error.is_retryable is True

    def test_ssrf_block_maps_permanent(self) -> None:
        conn = _make_conn()

        async def _block(*args: Any, **kwargs: Any) -> None:
            raise SSRFBlockedError("https://evil.example.com", "BLOCKED_PORT", "port 22")

        conn.connect = AsyncMock(side_effect=_block)
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "EGRESS_BLOCKED"
        assert result.error.is_retryable is False
        assert "BLOCKED_PORT" in result.error.error_message

    def test_build_accepts_optional_guard(self) -> None:
        guard = Mock(spec=SSRFGuard)
        conn = _make_conn()
        with patch("apps.worker.mcp_executor.MCPGatewayConnection", return_value=conn) as cls:
            build_mcp_executor("http://localhost:8000/mcp", guard=guard)

        cls.assert_called_once_with("http://localhost:8000/mcp", guard=guard)

    def test_timeout_code_in_error_text_restores_is_retryable(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(
            is_error=True, text="TIMEOUT: Cloud ERP request timed out"
        )
        executor = _build(conn)

        result = _call(executor)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "TIMEOUT"
        assert result.error.is_retryable is True

    def test_upstream_unavailable_code_restores_is_retryable(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(
            is_error=True, text="UPSTREAM_UNAVAILABLE: Cloud ERP unreachable"
        )
        executor = _build(conn)

        result = _call(executor)

        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_UNAVAILABLE"
        assert result.error.is_retryable is True

    def test_upstream_5xx_code_restores_is_retryable(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(
            is_error=True, text="UPSTREAM_503: Cloud ERP returned HTTP 503"
        )
        executor = _build(conn)

        result = _call(executor)

        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_503"
        assert result.error.is_retryable is True

    def test_upstream_4xx_code_stays_permanent(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(
            is_error=True, text="UPSTREAM_400: Cloud ERP returned HTTP 400"
        )
        executor = _build(conn)

        result = _call(executor)

        assert result.error is not None
        assert result.error.error_code == "MCP_TOOL_ERROR"
        assert result.error.is_retryable is False

    def test_create_order_timeout_preserves_code_but_stays_permanent(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(
            is_error=True, text="TIMEOUT: Cloud ERP request timed out"
        )
        executor = _build(conn)

        result = _call(executor, tool_name="createOrder")

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "TIMEOUT"
        assert result.error.is_retryable is False

    def test_create_order_upstream_5xx_preserves_code_but_stays_permanent(self) -> None:
        conn = _make_conn()
        conn.execute_tool.return_value = _result(
            is_error=True, text="UPSTREAM_503: Cloud ERP returned HTTP 503"
        )
        executor = _build(conn)

        result = _call(executor, tool_name="createOrder")

        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_503"
        assert result.error.is_retryable is False

    def test_create_order_not_retried_by_async_retry_executor(self) -> None:
        conn = _make_conn()
        attempts: dict[str, int] = {"n": 0}

        async def _flaky(*args: Any, **kwargs: Any) -> Any:
            attempts["n"] += 1
            return _result(is_error=True, text="TIMEOUT: Cloud ERP request timed out")

        conn.execute_tool = AsyncMock(side_effect=_flaky)
        executor = _build(conn)
        sleep = AsyncMock(return_value=None)

        async def run() -> ToolResult:
            retry = AsyncRetryExecutor(RetryPolicy(base_delay_s=0.0), sleep=sleep)
            return await retry.execute(
                lambda: executor("createOrder", {"product_id": 10094}),
                max_retries=2,
            )

        result = asyncio.run(run())

        assert result.status == "FAILED"
        assert attempts["n"] == 1
        sleep.assert_not_awaited()

    def test_async_retry_executor_retries_retryable_mcp_error(self) -> None:
        conn = _make_conn()
        attempts: dict[str, int] = {"n": 0}

        async def _flaky(*args: Any, **kwargs: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return _result(is_error=True, text="TIMEOUT: Cloud ERP request timed out")
            return _result(structured_content={"ok": True})

        conn.execute_tool = AsyncMock(side_effect=_flaky)
        executor = _build(conn)
        sleep = AsyncMock(return_value=None)

        async def run() -> ToolResult:
            retry = AsyncRetryExecutor(RetryPolicy(base_delay_s=0.0), sleep=sleep)
            return await retry.execute(
                lambda: executor("getProductByName", {"name": "苹果"}),
                max_retries=2,
            )

        result = asyncio.run(run())

        assert result.status == "SUCCEEDED"
        assert attempts["n"] == 2
        sleep.assert_awaited_once()
