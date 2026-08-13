"""SSRF egress wiring tests — the MCP gateway enforces the guard at both boundaries.

Task 5.3 implemented the SSRF guard as a pure module with unit tests; this
suite proves the gateway actually wires it into the production path: registering
a server rejects URLs that fail the egress policy (422 + security_events row)
and a connection built with a guard cannot establish a session to a blocked
URL (fail-closed).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from erp_copilot.domain.entities import SecurityEvent
from erp_copilot.infrastructure.database import get_session
from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard


def _fake_resolver(host: str) -> list[str]:
    """Deterministic stand-in for socket.getaddrinfo (offline integration test).

    partner.erp.example.com answers with an RFC1918 address — a DNS rebinding
    attack against the internal network.
    """
    return {
        "api.erp.example.com": ["93.184.216.34"],
        "partner.erp.example.com": ["10.0.0.5"],
    }.get(host, [])


def _guard() -> SSRFGuard:
    """Pre-registered egress policy: only https to the two ERP hosts is allowed."""
    return SSRFGuard(
        SSRFConfig(
            allowed_schemes=frozenset({"https"}),
            allowed_hosts=frozenset({"api.erp.example.com", "partner.erp.example.com"}),
            allowed_ports=frozenset({443}),
        ),
        resolve=_fake_resolver,
    )


def _security_events() -> list[SecurityEvent]:
    session = get_session()
    try:
        return session.query(SecurityEvent).all()
    finally:
        session.close()


class TestRegisterServerEndpoint:
    """POST /servers must validate the URL against the egress policy."""

    def _client(self) -> TestClient:
        from apps.mcp_gateway.gateway import create_gateway_app

        return TestClient(create_gateway_app(guard=_guard()))

    def test_register_allowlisted_host_returns_201_and_lists(self) -> None:
        client = self._client()

        response = client.post(
            "/servers", json={"name": "erp", "url": "https://api.erp.example.com"}
        )

        assert response.status_code == 201
        assert response.json() == {
            "name": "erp",
            "url": "https://api.erp.example.com",
            "status": "registered",
        }
        assert "erp" in client.get("/servers").json()["servers"]
        assert _security_events() == []

    def test_register_dns_rebinding_url_blocked_with_422_and_security_event(self) -> None:
        client = self._client()
        url = "https://partner.erp.example.com"

        response = client.post("/servers", json={"name": "evil", "url": url})

        assert response.status_code == 422
        events = _security_events()
        assert len(events) == 1
        assert events[0].attack_type == "SSRF"
        assert events[0].layer == "ssrf_guard"
        assert events[0].disposition == "blocked"
        assert events[0].input_summary == url
        assert events[0].run_id is None
        assert client.get("/servers").json()["servers"] == []

    def test_register_non_allowlisted_host_blocked_with_422_and_event(self) -> None:
        client = self._client()

        response = client.post("/servers", json={"name": "evil", "url": "https://evil.example.com"})

        assert response.status_code == 422
        events = _security_events()
        assert len(events) == 1
        assert events[0].attack_type == "SSRF"
        assert events[0].disposition == "blocked"
        assert client.get("/servers").json()["servers"] == []


class TestMCPGatewayConnectionSSRFIntegration:
    """A guarded connection must fail closed before any session is built."""

    def test_connect_to_dns_rebinding_url_raises_ssrf_blocked(self) -> None:
        from erp_copilot.tools.mcp_gateway import MCPGatewayConnection, SSRFBlockedError

        conn = MCPGatewayConnection("https://partner.erp.example.com", guard=_guard())

        with pytest.raises(SSRFBlockedError) as exc_info:
            asyncio.run(conn.connect())

        assert exc_info.value.reason == "BLOCKED_IP"
        assert conn.is_connected is False
