"""MCP-backed tool executor for the worker's agent graph.

Real MCP transport in the worker's main path: ``build_mcp_executor`` returns an
executor with the same ``async (tool_name, arguments) -> ToolResult`` contract
as the in-process simulator (apps/worker/executor.py) and the cloud HTTP
executor, so ``build_worker_graph(executor=...)`` accepts it with no other
change. The connection is lazy — the first tool call performs the MCP
initialize handshake over Streamable HTTP and later calls reuse the session.

Result mapping (docs/06): the MCP server's CallToolResult is normalized into the
internal ToolResult shape. ``is_error`` (the server executed and returned a
definitive error, e.g. product not found) becomes a permanent FAILED; a
successful result's ``structured_content`` becomes the data payload; a
connect/transport failure becomes a retryable UPSTREAM_UNAVAILABLE so the
graph's AsyncRetryExecutor drives the retry budget; an SSRF block stays
permanent (policy denies the URL — retrying cannot change that).

Lifecycle note: the worker runs each run on its own executor, so one MCP
connection lives per run (one initialize handshake). The graph executes steps
inside asyncio.gather child tasks, and the MCP client's anyio task group is
task-bound, so ``close()`` after the graph would tear the session down from a
different task than it was entered in — the worker therefore relies on
event-loop/process teardown to release the connection, exactly as execute_run
does today.
"""

from __future__ import annotations

import json
from typing import Any, cast

from mcp.types import CallToolResult, TextContent

from erp_copilot.security.ssrf_guard import SSRFGuard
from erp_copilot.tools.mcp_gateway import MCPGatewayConnection, SSRFBlockedError
from erp_copilot.tools.tool_result import ToolResult


def _text_content(result: CallToolResult) -> str | None:
    """Return the first text block of a result, or None if it has no text."""
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    return None


def _data_from_result(result: CallToolResult) -> dict[str, Any]:
    """Extract a successful tool's data payload into a dict.

    The SDK exposes typed ``structured_content`` when the tool returns a dict;
    otherwise the text content is parsed as JSON, with a plain-text fallback
    wrapped as {"text": ...} so callers always receive a dict.
    """
    if isinstance(result.structured_content, dict):
        return result.structured_content
    text = _text_content(result)
    if text is not None:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return {"text": text}
    return {}


class MCPToolExecutor:
    """Execute agent tool calls against one MCP server over the real transport.

    Wraps a lazily-connecting :class:`MCPGatewayConnection` behind the worker's
    ``(tool_name, arguments) -> ToolResult`` executor contract. Like the other
    executors it never raises: every outbound outcome (success, server error,
    policy block, transport failure) becomes a ToolResult.
    """

    def __init__(self, url: str, *, guard: SSRFGuard | None = None) -> None:
        self._connection = MCPGatewayConnection(url, guard=guard)

    @property
    def connection(self) -> MCPGatewayConnection:
        return self._connection

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute one tool; never raises (the verify node needs a StepResult)."""
        try:
            result = await self._call(tool_name, arguments)
        except SSRFBlockedError as exc:
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="EGRESS_BLOCKED",
                error_message=str(exc),
                is_retryable=False,
            )
        except Exception as exc:
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="UPSTREAM_UNAVAILABLE",
                error_message=f"MCP server unreachable: {exc}",
                is_retryable=True,
            )
        return self._map_result(tool_name, result)

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> CallToolResult:
        if not self._connection.is_connected:
            await self._connection.connect()
        raw = await self._connection.execute_tool(tool_name, arguments)
        # MCPGatewayConnection.execute_tool is typed Any (it wraps ClientSession);
        # the SDK guarantees a CallToolResult back from call_tool.
        return cast(CallToolResult, raw)

    def _map_result(self, tool_name: str, result: CallToolResult) -> ToolResult:
        if result.is_error:
            message = _text_content(result) or "MCP server reported an error"
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="MCP_TOOL_ERROR",
                error_message=message,
                is_retryable=False,
            )
        return ToolResult.success(tool_version_id=tool_name, data=_data_from_result(result))

    async def close(self) -> None:
        """Tear down the MCP session, releasing the transport and http client."""
        await self._connection.disconnect()


def build_mcp_executor(url: str, *, guard: SSRFGuard | None = None) -> MCPToolExecutor:
    """Build the MCP-backed executor for ``build_worker_graph(executor=...)``.

    *url* is the operator-configured MCP server endpoint (never prompt-derived),
    so — like the cloud HTTP executor — there is no SSRF surface from untrusted
    input; a *guard* may still be passed by callers that want the egress policy
    enforced (the e2e tests do, proving the block maps to a permanent failure).
    """
    return MCPToolExecutor(url, guard=guard)
