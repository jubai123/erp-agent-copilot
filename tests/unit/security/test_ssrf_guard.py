"""Unit tests for security/ssrf_guard.py — task 5.3.

The guard is a pure, deterministic pre-request check (docs/06 §6): it
validates scheme, rejects URL credentials, restricts ports, enforces a
pre-registered host allowlist, then resolves the host and rejects loopback /
private / link-local / multicast / reserved addresses. The resolver is
injected so DNS rebinding can be simulated deterministically. Block events are
persisted to security_events via record_security_event. Tests run against
in-memory SQLite, the same pattern as test_approval.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import SecurityEvent
from erp_copilot.security.ssrf_guard import (
    SSRFConfig,
    SSRFGuard,
    SSRFVerdict,
    is_internal_ip,
    record_security_event,
)


class _FakeResolver:
    """Deterministic DNS: host → addresses, defaulting to a public IP."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping
        self._default = ["93.184.216.34"]  # example.com — globally routable

    def __call__(self, host: str) -> list[str]:
        return self._mapping.get(host, self._default)


def _guard(
    *,
    allowed_hosts: set[str] | None = None,
    allowed_ports: set[int] | None = None,
    trusted_internal_hosts: set[str] | None = None,
    resolve: object | None = None,
) -> SSRFGuard:
    return SSRFGuard(
        SSRFConfig(
            allowed_hosts=frozenset(allowed_hosts or ()),
            allowed_ports=(
                frozenset(allowed_ports) if allowed_ports is not None else frozenset({443})
            ),
            trusted_internal_hosts=frozenset(trusted_internal_hosts or ()),
        ),
        resolve=resolve,
    )


class TestIsInternalIP:
    def test_loopback_ipv4(self) -> None:
        assert is_internal_ip("127.0.0.1") is True

    def test_private_rfc1918(self) -> None:
        assert is_internal_ip("10.1.2.3") is True
        assert is_internal_ip("172.16.0.1") is True
        assert is_internal_ip("192.168.1.1") is True

    def test_private_boundary_172_32_is_public(self) -> None:
        # 172.32.0.0/12 is 172.16–172.31; 172.32 falls outside it.
        assert is_internal_ip("172.32.0.1") is False

    def test_link_local_multicast_reserved(self) -> None:
        assert is_internal_ip("169.254.10.10") is True
        assert is_internal_ip("224.0.0.1") is True
        assert is_internal_ip("240.0.0.1") is True
        assert is_internal_ip("0.0.0.0") is True

    def test_public_ipv4(self) -> None:
        assert is_internal_ip("8.8.8.8") is False
        assert is_internal_ip("93.184.216.34") is False

    def test_ipv6_internal(self) -> None:
        assert is_internal_ip("::1") is True
        assert is_internal_ip("fe80::1") is True
        assert is_internal_ip("fc00::1") is True
        assert is_internal_ip("ff02::1") is True

    def test_ipv6_public(self) -> None:
        assert is_internal_ip("2606:2800:220:1::1") is False


class TestScheme:
    def test_https_allowed(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        assert g.check("https://api.example.com/").allowed is True

    def test_http_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        verdict = g.check("http://api.example.com/")
        assert verdict.allowed is False
        assert verdict.reason == "BLOCKED_SCHEME"

    def test_ftp_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        assert g.check("ftp://api.example.com/").reason == "BLOCKED_SCHEME"


class TestHostAllowlist:
    def test_non_whitelisted_host_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        verdict = g.check("https://evil.example.com/")
        assert verdict.allowed is False
        assert verdict.reason == "HOST_NOT_ALLOWED"

    def test_whitelisted_host_allowed(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        assert g.check("https://api.example.com/").allowed is True

    def test_host_port_granularity(self) -> None:
        # Only api.example.com:8443 is registered; the bare host is not.
        g = _guard(
            allowed_hosts={"api.example.com:8443"},
            allowed_ports={443, 8443},
            resolve=_FakeResolver({}),
        )
        assert g.check("https://api.example.com:8443/").allowed is True
        assert g.check("https://api.example.com/").reason == "HOST_NOT_ALLOWED"


class TestPort:
    def test_non_allowed_port_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        verdict = g.check("https://api.example.com:444/")
        assert verdict.allowed is False
        assert verdict.reason == "BLOCKED_PORT"

    def test_extra_allowed_port(self) -> None:
        g = _guard(
            allowed_hosts={"api.example.com"},
            allowed_ports={443, 8443},
            resolve=_FakeResolver({}),
        )
        assert g.check("https://api.example.com:8443/").allowed is True


class TestInternalIPLiteral:
    def test_loopback_literal_blocked_even_when_allowlisted(self) -> None:
        verdict = _guard(allowed_hosts={"127.0.0.1"}).check("https://127.0.0.1/")
        assert verdict.allowed is False
        assert verdict.reason == "BLOCKED_IP"

    def test_private_literals_blocked(self) -> None:
        assert _guard(allowed_hosts={"10.0.0.5"}).check("https://10.0.0.5/").reason == "BLOCKED_IP"
        assert (
            _guard(allowed_hosts={"192.168.1.10"}).check("https://192.168.1.10/").reason
            == "BLOCKED_IP"
        )

    def test_public_literal_allowed(self) -> None:
        assert _guard(allowed_hosts={"8.8.8.8"}).check("https://8.8.8.8/").allowed is True


class TestDNSResolution:
    def test_public_resolution_allowed(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        assert g.check("https://api.example.com/").allowed is True

    def test_dns_rebinding_to_private_blocked(self) -> None:
        resolver = _FakeResolver({"api.example.com": ["10.0.0.9"]})
        g = _guard(allowed_hosts={"api.example.com"}, resolve=resolver)
        verdict = g.check("https://api.example.com/")
        assert verdict.allowed is False
        assert verdict.reason == "BLOCKED_IP"

    def test_any_internal_address_in_resolution_blocks(self) -> None:
        resolver = _FakeResolver({"api.example.com": ["93.184.216.34", "192.168.1.5"]})
        g = _guard(allowed_hosts={"api.example.com"}, resolve=resolver)
        assert g.check("https://api.example.com/").reason == "BLOCKED_IP"

    def test_empty_resolution_fails_closed(self) -> None:
        resolver = _FakeResolver({"api.example.com": []})
        g = _guard(allowed_hosts={"api.example.com"}, resolve=resolver)
        assert g.check("https://api.example.com/").reason == "RESOLUTION_FAILED"

    def test_trusted_internal_host_allowed(self) -> None:
        resolver = _FakeResolver({"erp-simulator.local": ["127.0.0.1"]})
        g = _guard(
            allowed_hosts={"erp-simulator.local"},
            trusted_internal_hosts={"erp-simulator.local"},
            resolve=resolver,
        )
        assert g.check("https://erp-simulator.local/").allowed is True

    def test_trusted_internal_still_needs_allowlist(self) -> None:
        resolver = _FakeResolver({"erp-simulator.local": ["127.0.0.1"]})
        g = _guard(trusted_internal_hosts={"erp-simulator.local"}, resolve=resolver)
        assert g.check("https://erp-simulator.local/").reason == "HOST_NOT_ALLOWED"


class TestUserInfo:
    def test_credentials_in_url_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        verdict = g.check("https://user:pass@api.example.com/")
        assert verdict.allowed is False
        assert verdict.reason == "BLOCKED_USERINFO"


class TestInvalidURL:
    def test_unparseable_input_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"})
        assert g.check("not a url").reason == "INVALID_URL"

    def test_missing_host_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"})
        assert g.check("https://").reason == "INVALID_URL"


class TestRedirect:
    def test_same_host_redirect_allowed(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        verdict = g.check_redirect("https://api.example.com/a", "https://api.example.com/b")
        assert verdict.allowed is True

    def test_cross_host_redirect_blocked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        verdict = g.check_redirect("https://api.example.com/a", "https://evil.example.com/b")
        assert verdict.allowed is False
        assert verdict.reason == "REDIRECT_CROSS_HOST"

    def test_redirect_target_rechecked(self) -> None:
        g = _guard(allowed_hosts={"api.example.com"}, resolve=_FakeResolver({}))
        assert (
            g.check_redirect("https://api.example.com/a", "https://api.example.com:444/b").reason
            == "BLOCKED_PORT"
        )


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    SecurityEvent.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


class TestSecurityEventRecording:
    def test_record_block_event(self, session: Session) -> None:
        verdict = SSRFVerdict(allowed=False, reason="BLOCKED_IP", detail="10.0.0.5")
        event = record_security_event(
            session, run_id="r1", url="https://10.0.0.5/", verdict=verdict
        )
        row = session.get(SecurityEvent, event.id)
        assert row is not None
        assert row.attack_type == "SSRF"
        assert row.layer == "ssrf_guard"
        assert row.severity == "HIGH"
        assert row.input_summary == "https://10.0.0.5/"
        assert row.disposition == "blocked"
        assert row.run_id == "r1"

    def test_record_block_event_without_run(self, session: Session) -> None:
        event = record_security_event(
            session,
            run_id=None,
            url="https://10.0.0.5/",
            verdict=SSRFVerdict(allowed=False, reason="BLOCKED_IP"),
        )
        row = session.get(SecurityEvent, event.id)
        assert row is not None
        assert row.run_id is None

    def test_guard_block_then_record(self, session: Session) -> None:
        g = _guard(allowed_hosts={"127.0.0.1"})
        verdict = g.check("https://127.0.0.1/")
        assert verdict.allowed is False
        record_security_event(session, run_id="r1", url="https://127.0.0.1/", verdict=verdict)
        assert session.query(SecurityEvent).count() == 1
