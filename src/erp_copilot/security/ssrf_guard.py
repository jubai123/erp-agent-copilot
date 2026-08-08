"""SSRF guard and egress control — task 5.3.

A pure, deterministic pre-request check (docs/06 §6): validates the scheme,
rejects URL credentials, restricts ports, enforces a pre-registered host
allowlist, then resolves the host and rejects loopback / private / link-local
/ multicast / reserved addresses. Every resolved address is checked, so a
hostname that answers with an internal address — DNS rebinding — is blocked.
The resolver is injected so tests can drive DNS deterministically; the guard
itself never opens a connection.

Redirects may only stay on the same host and are re-checked on every hop
(:meth:`SSRFGuard.check_redirect`). Block events are persisted to
security_events by :func:`record_security_event`; the guard stays pure and the
caller decides the disposition.

Note a TOCTOU limit: the address is verified here but the later connection
performs its own resolution, so a resolver that alternates answers between
check and connect can still slip through — closing that gap requires the
executor to connect to the verified address directly, which is out of scope.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from erp_copilot.domain.entities import SecurityEvent

# Address families that must never be reached from the server (docs/06 §6
# rule 3): IPv4 "this"/unspecified, RFC1918 private, loopback, link-local,
# multicast and reserved; IPv6 unspecified, loopback, ULA, link-local and
# multicast. Split by family so the containment check stays type-safe.
_IPV4_INTERNAL: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("224.0.0.0/4"),
    ipaddress.IPv4Network("240.0.0.0/4"),
)
_IPV6_INTERNAL: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("ff00::/8"),
)

_SCHEME_DEFAULT_PORTS: dict[str, int] = {"https": 443}


def is_internal_ip(ip_str: str) -> bool:
    """True when the address is loopback/private/link-local/multicast/etc.

    The exact set is the explicit table above — public addresses (e.g.
    8.8.8.8, 93.184.216.34) are not internal.
    """
    ip = ipaddress.ip_address(ip_str)
    networks: tuple[ipaddress.IPv4Network, ...] | tuple[ipaddress.IPv6Network, ...] = (
        _IPV4_INTERNAL if ip.version == 4 else _IPV6_INTERNAL
    )
    return any(ip in network for network in networks)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _default_resolve(host: str) -> list[str]:
    """Resolve *host* to every distinct TCP address (de-duplicated)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos if isinstance(info[4][0], str)})


@dataclass(frozen=True)
class SSRFVerdict:
    """Outcome of a guard check; callers decide the disposition.

    reason is a stable machine-readable code (BLOCKED_SCHEME, HOST_NOT_ALLOWED,
    BLOCKED_IP, ...) and detail the offending value.
    """

    allowed: bool
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SSRFConfig:
    """Pre-registered egress policy (docs/06 §6 rules 1, 2, 6).

    allowed_hosts entries are ``host`` or ``host:port``; a bare host grants
    every allowed port. trusted_internal_hosts marks explicitly authorized
    internal endpoints (e.g. the ERP simulator) whose addresses may be
    non-public — they still must be in allowed_hosts.
    """

    allowed_schemes: frozenset[str] = frozenset({"https"})
    allowed_hosts: frozenset[str] = frozenset()
    allowed_ports: frozenset[int] = frozenset({443})
    trusted_internal_hosts: frozenset[str] = frozenset()


class SSRFGuard:
    """Pure egress check with an injected resolver (default: real DNS)."""

    def __init__(
        self,
        config: SSRFConfig,
        *,
        resolve: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._config = config
        self._resolve = resolve if resolve is not None else _default_resolve

    def check(self, url: str) -> SSRFVerdict:
        """Validate *url* for egress; never blocks on network I/O."""
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None:
            return SSRFVerdict(False, reason="INVALID_URL", detail=url)
        if parsed.scheme not in self._config.allowed_schemes:
            return SSRFVerdict(False, reason="BLOCKED_SCHEME", detail=parsed.scheme)
        if parsed.username is not None or parsed.password is not None:
            return SSRFVerdict(False, reason="BLOCKED_USERINFO", detail=url)
        try:
            explicit_port = parsed.port
        except ValueError:
            return SSRFVerdict(False, reason="INVALID_URL", detail=url)
        port = (
            explicit_port if explicit_port is not None else _SCHEME_DEFAULT_PORTS.get(parsed.scheme)
        )
        if port is None or port not in self._config.allowed_ports:
            return SSRFVerdict(False, reason="BLOCKED_PORT", detail=str(port))
        if not self._host_allowed(host, port):
            return SSRFVerdict(False, reason="HOST_NOT_ALLOWED", detail=host)
        return self._check_addresses(host)

    def check_redirect(self, original_url: str, target_url: str) -> SSRFVerdict:
        """A redirect may only follow to the same host, then is re-checked."""
        if urlparse(original_url).hostname != urlparse(target_url).hostname:
            return SSRFVerdict(False, reason="REDIRECT_CROSS_HOST", detail=target_url)
        return self.check(target_url)

    def _host_allowed(self, host: str, port: int) -> bool:
        return host in self._config.allowed_hosts or f"{host}:{port}" in self._config.allowed_hosts

    def _check_addresses(self, host: str) -> SSRFVerdict:
        if host in self._config.trusted_internal_hosts:
            return SSRFVerdict(True)
        try:
            addresses: list[str] = [host] if _is_ip_literal(host) else self._resolve(host)
        except OSError:
            return SSRFVerdict(False, reason="RESOLUTION_FAILED", detail=host)
        if not addresses:
            return SSRFVerdict(False, reason="RESOLUTION_FAILED", detail=host)
        for address in addresses:
            try:
                internal = is_internal_ip(address)
            except ValueError:
                return SSRFVerdict(False, reason="RESOLUTION_FAILED", detail=address)
            if internal:
                return SSRFVerdict(False, reason="BLOCKED_IP", detail=address)
        return SSRFVerdict(True)


def record_security_event(
    session: Session,
    *,
    run_id: str | None,
    url: str,
    verdict: SSRFVerdict,
) -> SecurityEvent:
    """Persist one SSRF block into security_events (docs/07).

    Called by the executor when the guard returns a blocked verdict; the
    verdict documents why for audit tooling built on the table later.
    """
    event = SecurityEvent(
        attack_type="SSRF",
        layer="ssrf_guard",
        severity="HIGH",
        input_summary=url,
        disposition="blocked",
        run_id=run_id,
    )
    session.add(event)
    session.commit()
    return event
