"""Unit tests for header-based identity and scope dependencies (task 5.1).

get_actor is exercised without a database by monkeypatching the module-level
get_session / resolve_user_scopes symbols; require_scope and require_run_tenant
are pure guards over an Actor and need no stubbing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from erp_copilot.security import dependencies


class _FakeSession:
    def close(self) -> None:
        """No-op so get_actor's finally-block is a no-op in unit tests."""


def _header_settings() -> SimpleNamespace:
    return SimpleNamespace(auth_mode="header", api_key_pepper="")


def _api_key_settings() -> SimpleNamespace:
    return SimpleNamespace(auth_mode="api_key", api_key_pepper="")


def test_get_actor_missing_tenant_header_is_401(monkeypatch) -> None:
    monkeypatch.setattr(dependencies, "_settings", lambda: _header_settings())
    monkeypatch.setattr(dependencies, "get_session", lambda: _FakeSession())

    with pytest.raises(HTTPException) as exc:
        dependencies.get_actor(x_tenant_id=None, x_user_id=None)
    assert exc.value.status_code == 401


def test_get_actor_resolves_tenant_user_and_scopes(monkeypatch) -> None:
    fake = _FakeSession()
    monkeypatch.setattr(dependencies, "_settings", lambda: _header_settings())
    monkeypatch.setattr(dependencies, "get_session", lambda: fake)
    monkeypatch.setattr(
        dependencies,
        "resolve_user_scopes",
        lambda session, tenant_id, user_id: {"order:write", "product:read"},
    )

    actor = dependencies.get_actor(x_tenant_id="tenant-1", x_user_id="user-1")

    assert actor.tenant_id == "tenant-1"
    assert actor.user_id == "user-1"
    assert actor.scopes == frozenset({"order:write", "product:read"})


def test_get_actor_unknown_user_resolves_empty_scopes(monkeypatch) -> None:
    fake = _FakeSession()
    monkeypatch.setattr(dependencies, "_settings", lambda: _header_settings())
    monkeypatch.setattr(dependencies, "get_session", lambda: fake)
    monkeypatch.setattr(dependencies, "resolve_user_scopes", lambda s, t, u: set())

    actor = dependencies.get_actor(x_tenant_id="tenant-1", x_user_id="ghost")

    assert actor.user_id == "ghost"
    assert actor.scopes == frozenset()


def test_require_scope_passes_when_scope_present() -> None:
    actor = dependencies.Actor(tenant_id="t1", user_id="u1", scopes=frozenset({"order:write"}))

    assert dependencies.require_scope("order:write")(actor=actor) is actor


def test_require_scope_forbids_when_scope_missing() -> None:
    actor = dependencies.Actor(tenant_id="t1", user_id="u1", scopes=frozenset({"product:read"}))

    with pytest.raises(HTTPException) as exc:
        dependencies.require_scope("order:write")(actor=actor)
    assert exc.value.status_code == 403


def test_require_run_tenant_accepts_same_tenant() -> None:
    actor = dependencies.Actor(tenant_id="t1", user_id="u1", scopes=frozenset())

    # Must not raise.
    dependencies.require_run_tenant("t1", actor)


def test_require_run_tenant_rejects_cross_tenant() -> None:
    actor = dependencies.Actor(tenant_id="t1", user_id="u1", scopes=frozenset())

    with pytest.raises(HTTPException) as exc:
        dependencies.require_run_tenant("t2", actor)
    assert exc.value.status_code == 403


class TestGetActorApiKeyMode:
    """api_key mode: identity derives from the key, never from client headers."""

    def test_missing_key_is_401(self, monkeypatch) -> None:
        monkeypatch.setattr(dependencies, "_settings", lambda: _api_key_settings())

        with pytest.raises(HTTPException) as exc:
            dependencies.get_actor(x_api_key=None, x_tenant_id=None, x_user_id=None)
        assert exc.value.status_code == 401

    def test_invalid_key_is_401(self, monkeypatch) -> None:
        monkeypatch.setattr(dependencies, "_settings", lambda: _api_key_settings())
        monkeypatch.setattr(dependencies, "get_session", lambda: _FakeSession())
        monkeypatch.setattr(dependencies, "verify_api_key", lambda s, k, pepper: None)

        with pytest.raises(HTTPException) as exc:
            dependencies.get_actor(x_api_key="erp_live_bad", x_tenant_id=None, x_user_id=None)
        assert exc.value.status_code == 401

    def test_identity_derives_from_key(self, monkeypatch) -> None:
        fake = _FakeSession()
        monkeypatch.setattr(dependencies, "_settings", lambda: _api_key_settings())
        monkeypatch.setattr(dependencies, "get_session", lambda: fake)
        monkeypatch.setattr(
            dependencies,
            "verify_api_key",
            lambda s, k, pepper: SimpleNamespace(tenant_id="t1", user_id="u1"),
        )
        monkeypatch.setattr(
            dependencies,
            "resolve_user_scopes",
            lambda session, tenant_id, user_id: {"order:write"},
        )

        actor = dependencies.get_actor(x_api_key="erp_live_ok", x_tenant_id=None, x_user_id=None)

        assert actor.tenant_id == "t1"
        assert actor.user_id == "u1"
        assert actor.scopes == frozenset({"order:write"})

    def test_conflicting_tenant_header_is_401(self, monkeypatch) -> None:
        fake = _FakeSession()
        monkeypatch.setattr(dependencies, "_settings", lambda: _api_key_settings())
        monkeypatch.setattr(dependencies, "get_session", lambda: fake)
        monkeypatch.setattr(
            dependencies,
            "verify_api_key",
            lambda s, k, pepper: SimpleNamespace(tenant_id="t1", user_id="u1"),
        )
        monkeypatch.setattr(dependencies, "resolve_user_scopes", lambda s, t, u: set())

        with pytest.raises(HTTPException) as exc:
            dependencies.get_actor(x_api_key="erp_live_ok", x_tenant_id="t2", x_user_id="u1")
        assert exc.value.status_code == 401

    def test_conflicting_user_header_is_401(self, monkeypatch) -> None:
        fake = _FakeSession()
        monkeypatch.setattr(dependencies, "_settings", lambda: _api_key_settings())
        monkeypatch.setattr(dependencies, "get_session", lambda: fake)
        monkeypatch.setattr(
            dependencies,
            "verify_api_key",
            lambda s, k, pepper: SimpleNamespace(tenant_id="t1", user_id="u1"),
        )
        monkeypatch.setattr(dependencies, "resolve_user_scopes", lambda s, t, u: set())

        with pytest.raises(HTTPException) as exc:
            dependencies.get_actor(x_api_key="erp_live_ok", x_tenant_id="t1", x_user_id="u2")
        assert exc.value.status_code == 401

    def test_matching_headers_are_allowed(self, monkeypatch) -> None:
        fake = _FakeSession()
        monkeypatch.setattr(dependencies, "_settings", lambda: _api_key_settings())
        monkeypatch.setattr(dependencies, "get_session", lambda: fake)
        monkeypatch.setattr(
            dependencies,
            "verify_api_key",
            lambda s, k, pepper: SimpleNamespace(tenant_id="t1", user_id="u1"),
        )
        monkeypatch.setattr(
            dependencies,
            "resolve_user_scopes",
            lambda s, t, u: {"product:read"},
        )

        actor = dependencies.get_actor(x_api_key="erp_live_ok", x_tenant_id="t1", x_user_id="u1")

        assert actor.tenant_id == "t1"
        assert actor.user_id == "u1"
