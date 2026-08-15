"""Fail-closed guards for the production compose stack (infra/docker-compose.yml).

The compose file is the deployment surface: a misconfig here silently ships
the API in development header mode, Grafana with an admin/admin login, or
exposes the observability UIs to the public network. None of it is executable
Python, so these guards parse the file the way compose would and assert the
fail-closed defaults production relies on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE = _REPO_ROOT / "infra" / "docker-compose.yml"


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


class TestAuthFailClosedDefault:
    def test_api_auth_default_is_api_key(self) -> None:
        env = _compose()["services"]["api"]["environment"]
        # Production must fail closed: the compose default is api_key, never the
        # development-only header mode that trusts client-supplied headers.
        assert env["AUTH_MODE"] == "${AUTH_MODE:-api_key}"

    def test_api_auth_never_defaults_to_header(self) -> None:
        env = _compose()["services"]["api"]["environment"]
        # header mode would let anyone impersonate a tenant/user with plain
        # headers — it must be an explicit opt-in, not a silent default.
        assert ":-header" not in env["AUTH_MODE"]


class TestGrafanaPasswordFailClosed:
    def test_grafana_admin_password_is_required(self) -> None:
        env = _compose()["services"]["grafana"]["environment"]
        # `:?` makes compose abort with a clear message instead of defaulting to
        # a predictable password.
        assert "${GRAFANA_ADMIN_PASSWORD:?" in env["GF_SECURITY_ADMIN_PASSWORD"]

    def test_grafana_has_no_weak_default_password(self) -> None:
        env = _compose()["services"]["grafana"]["environment"]
        assert ":-admin" not in env["GF_SECURITY_ADMIN_PASSWORD"]


class TestObservabilityPortsHostBound:
    @pytest.mark.parametrize(
        ("service", "published_port"),
        [
            ("prometheus", "127.0.0.1:9090:9090"),
            ("grafana", "127.0.0.1:3000:3000"),
        ],
    )
    def test_observability_ports_bind_to_loopback(self, service: str, published_port: str) -> None:
        # Published ports default to 0.0.0.0 (public NIC). Production binds the
        # observability UIs to loopback so they are reachable only via an SSH
        # tunnel, never from the public interface.
        assert published_port in _compose()["services"][service]["ports"]


class TestInfrastructurePortsHostBound:
    @pytest.mark.parametrize(
        ("service", "published_port"),
        [
            ("postgres", "127.0.0.1:5432:5432"),
            ("redis", "127.0.0.1:6379:6379"),
            ("worker", "127.0.0.1:8003:8003"),
        ],
    )
    def test_infrastructure_ports_bind_to_loopback(self, service: str, published_port: str) -> None:
        # Nothing outside the host should reach the database, the cache, or the
        # worker's metrics endpoint directly. Containers still reach them over
        # the compose network regardless of the host binding, so loopback-only
        # publishing costs nothing internally.
        assert published_port in _compose()["services"][service]["ports"]
