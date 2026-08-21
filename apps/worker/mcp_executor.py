"""MCP-backed tool executor for the worker's agent graph.

Real MCP transport in the worker's main path: ``build_mcp_executor`` returns an
executor with the same ``async (tool_name, arguments) -> ToolResult`` contract
as the in-process simulator (apps/worker/executor.py) and the cloud HTTP
executor, so ``build_worker_graph(executor=...)`` accepts it with no other
change. The connection is lazy — the first tool call performs the MCP
initialize handshake over Streamable HTTP and later calls reuse the session.

Result mapping (docs/06): the MCP server's CallToolResult is normalized into the
internal ToolResult shape. ``is_error`` (the server executed and returned a
definitive error, e.g. product not found) becomes a FAILED. When the error text
carries a known transient code (erp_mcp_server embeds the executor's
``TIMEOUT``/``UPSTREAM_UNAVAILABLE``/``UPSTREAM_5xx`` as a message prefix), the
code is preserved as the failure's ``error_code`` for every tool — the recovery
gate (recover_or_replan) needs it to tell an ambiguous write (response lost but
cloud may have applied it) from a plain error. Only the ``is_retryable`` flag is
restored, and only for the read tools (``_READ_TOOL_NAMES``): the cloud-ERP
createOrder has no server-side idempotency, so its transient codes stay
non-retryable and route to the recovery sink where the ambiguous-write gate gives
up for a human instead of auto-retrying or replanning. A successful result's
``structured_content`` becomes the data payload; a connect/transport failure
becomes a retryable UPSTREAM_UNAVAILABLE; an SSRF block stays permanent (policy
denies the URL — retrying cannot change that).

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
import re
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


# The cloud HTTP executor's retryable failure codes (apps/worker/executor.py):
# exact matches plus UPSTREAM_<status> where 500 <= status < 600. MCP has no
# retry signal, so erp_mcp_server embeds the code as the error-text prefix and
# the worker parses it back out — mirroring the HTTP path's retryability.
_KNOWN_RETRYABLE_CODES = frozenset({"TIMEOUT", "UPSTREAM_UNAVAILABLE"})
_UPSTREAM_STATUS_RE = re.compile(r"^UPSTREAM_(\d{3})$")

# Only these side-effect-free read tools get is_retryable restoration. The
# transient code itself is preserved as error_code for every tool (the recovery
# gate needs it to classify ambiguity); this allowlist decides only whether the
# step is auto-retried. The cloud ERP's createOrder has no server-side
# idempotency, so an ambiguous timeout on it must never auto-retry (a
# lost-response retry could duplicate a real order); it stays non-retryable and
# the recovery path gives up for a human, reconciling via a getOrderByOrderId
# read-back. An allowlist — not a blocklist — means any future write tool also
# defaults to non-retryable, no maintenance needed.
_READ_TOOL_NAMES = frozenset(
    {
        "getProductByName",
        "getProductById",
        "getProductSubstitutes",
        "getProductSubstitutesByName",
        "getBatchProductByProductIds",
        "getSupplierByStatus",
        "querySuppliersByDeliveryRegion",
        "getSupplierByName",
        "getSupplierById",
        "getOrderByOrderId",
        "getOrdersBySupplierId",
        "getByProductId",
        "getByOrderStatus",
        "getByTimeRange",
    }
)


def _code_from_message(message: str) -> str | None:
    """Return a known transient error code from an MCP error message, else None.

    erp_mcp_server raises ``ValueError(f"{code}: {message}")`` (SDK surfaces it
    as is_error=True with that text), so a transient cloud failure arrives as
    e.g. "TIMEOUT: ..." or "UPSTREAM_503: ...". The code is preserved as the
    failure's ``error_code`` for every tool — the recovery gate matches on it —
    while retryability is decided separately by ``_READ_TOOL_NAMES``. Any other
    message shape stays a permanent MCP_TOOL_ERROR.
    """
    code, sep, _ = message.partition(": ")
    if not sep:
        return None
    if code in _KNOWN_RETRYABLE_CODES:
        return code
    match = _UPSTREAM_STATUS_RE.match(code)
    if match is not None and 500 <= int(match.group(1)) < 600:
        return code
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
            code = _code_from_message(message)
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code=code or "MCP_TOOL_ERROR",
                error_message=message,
                is_retryable=code is not None and tool_name in _READ_TOOL_NAMES,
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
