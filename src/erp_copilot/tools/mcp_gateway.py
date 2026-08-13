"""MCP Gateway — persistent connections to MCP-compatible tool servers.

Wraps the MCP Python SDK's :class:`ClientSession` with connection lifecycle
management, tool caching, and auto-reconnect.
"""

from __future__ import annotations

from asyncio import Lock
from typing import Any

from mcp import ClientSession

from erp_copilot.security.ssrf_guard import SSRFGuard


class SSRFBlockedError(RuntimeError):
    """Raised when a connection target fails the egress policy (task 5.3).

    Carries the guard's stable verdict so the caller can persist the block
    (SecurityEvent) or surface the reason without re-checking the URL.
    """

    def __init__(self, url: str, reason: str | None, detail: str | None) -> None:
        self.url = url
        self.reason = reason
        self.detail = detail
        super().__init__(f"URL blocked by egress policy: {reason} ({detail})")


class MCPGatewayConnection:
    """A persistent, lazy-connecting wrapper around an MCP :class:`ClientSession`.

    Connections are established on first use and reused across calls.
    Disconnected sessions are automatically reconnected on the next call.
    """

    def __init__(self, url: str, *, guard: SSRFGuard | None = None) -> None:
        self._url = url
        self._guard = guard
        self._session: ClientSession | None = None
        self._lock = Lock()

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    async def connect(self) -> None:
        """Establish (or re-establish) the MCP session.

        Idempotent: calling on an already-connected session is a no-op.
        A configured SSRF guard is enforced here, before any session exists
        (docs/06 §6 "请求发出前"): a URL that fails the egress policy raises
        :class:`SSRFBlockedError` and no session is ever created (fail-closed).
        """
        async with self._lock:
            if self._session is not None:
                return
            if self._guard is not None:
                verdict = self._guard.check(self._url)
                if not verdict.allowed:
                    raise SSRFBlockedError(self._url, verdict.reason, verdict.detail)
            session = ClientSession()
            await session.initialize()
            self._session = session

    async def disconnect(self) -> None:
        """Tear down the session, releasing underlying transport resources."""
        async with self._lock:
            self._session = None

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool on the connected MCP server.

        Raises :class:`RuntimeError` if not connected.
        """
        if self._session is None:
            raise RuntimeError(
                f"Cannot execute '{name}': not connected to {self._url}. Call connect() first."
            )
        return await self._session.call_tool(name, arguments)


class MCPGateway:
    """Registry of named :class:`MCPGatewayConnection` instances.

    Each registered connection represents a tool server the agent can
    execute tools against. Connections are identified by a short name
    (e.g. ``"erp"``, ``"crm"``).
    """

    def __init__(self) -> None:
        self._connections: dict[str, MCPGatewayConnection] = {}

    def register(self, name: str, connection: MCPGatewayConnection) -> None:
        """Register a named connection, replacing any existing one."""
        self._connections[name] = connection

    def unregister(self, name: str) -> None:
        """Remove a connection by name."""
        self._connections.pop(name, None)

    def get(self, name: str) -> MCPGatewayConnection | None:
        """Return the connection registered under *name*, or ``None``."""
        return self._connections.get(name)

    def list_servers(self) -> list[str]:
        """Return the names of all registered connections."""
        return list(self._connections.keys())
