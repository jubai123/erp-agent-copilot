"""FastAPI dependencies: actor identity + scope/tenant guards.

docs/06 §4 and docs/07 §10: the API must not trust client-submitted tenant,
user, or scope — they come from the authentication context and the role graph
in the database. In ``api_key`` mode (production default) the identity derives
from a verified ``X-API-Key`` bound to (tenant, user); the legacy ``X-Tenant-ID``
/ ``X-User-ID`` headers are accepted only when they match the key's identity
(anti-spoofing). In ``header`` mode (development only) the headers are trusted
directly. ``get_actor`` resolves the acting user's effective scopes once per
request; ``require_scope`` turns a missing scope into a 403;
``require_run_tenant`` rejects cross-tenant access to a resource. The worker's
policy_check node remains the authoritative plan-time scope gate — these
dependencies are the API-boundary fast-fail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, Header, HTTPException

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import get_session
from erp_copilot.security.api_keys import verify_api_key
from erp_copilot.security.rbac import resolve_user_scopes


@dataclass(frozen=True)
class Actor:
    """Authenticated identity for a single request (headers + DB role graph)."""

    tenant_id: str
    user_id: str | None
    scopes: frozenset[str]


@lru_cache(maxsize=1)
def _settings() -> Settings:
    """Cached Settings instance; auth_mode picks the header vs api_key path."""
    return Settings()  # type: ignore[call-arg]


def get_actor(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_tenant_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> Actor:
    """Resolve the acting user's identity and effective scopes.

    In ``api_key`` mode the identity derives from the verified key; in
    ``header`` mode (development) the headers are trusted directly.
    """
    settings = _settings()
    if settings.auth_mode == "api_key":
        return _actor_from_api_key(settings, x_api_key, x_tenant_id, x_user_id)
    return _actor_from_headers(x_tenant_id, x_user_id)


def _actor_from_headers(x_tenant_id: str | None, x_user_id: str | None) -> Actor:
    """Legacy development path: trust the client-supplied identity headers.

    A missing tenant header is unauthenticated (401) — there is no safe default
    tenant to fall back to. A missing user header resolves to an anonymous
    actor with no scopes (system-initiated runs).
    """
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-ID header")
    session = get_session()
    try:
        scopes = resolve_user_scopes(session, x_tenant_id, x_user_id or "")
    finally:
        session.close()
    return Actor(tenant_id=x_tenant_id, user_id=x_user_id, scopes=frozenset(scopes))


def _actor_from_api_key(
    settings: Settings,
    x_api_key: str | None,
    x_tenant_id: str | None,
    x_user_id: str | None,
) -> Actor:
    """Production path: identity derives from the verified API key.

    The key is bound to (tenant, user); the legacy headers are honoured only
    when they match the key's identity, so a client cannot spoof another actor.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    session = get_session()
    try:
        key = verify_api_key(session, x_api_key, pepper=settings.api_key_pepper)
        if key is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked X-API-Key")
        if x_tenant_id is not None and x_tenant_id != key.tenant_id:
            raise HTTPException(
                status_code=401, detail="X-Tenant-ID does not match the API key identity"
            )
        if x_user_id is not None and x_user_id != key.user_id:
            raise HTTPException(
                status_code=401, detail="X-User-ID does not match the API key identity"
            )
        scopes = resolve_user_scopes(session, key.tenant_id, key.user_id)
        return Actor(tenant_id=key.tenant_id, user_id=key.user_id, scopes=frozenset(scopes))
    finally:
        session.close()


def require_scope(scope: str) -> Callable[..., Actor]:
    """Dependency factory: reject the request when *scope* is not held."""

    def _guard(actor: Actor = Depends(get_actor)) -> Actor:
        if scope not in actor.scopes:
            raise HTTPException(status_code=403, detail=f"Missing required scope '{scope}'")
        return actor

    return _guard


def require_run_tenant(run_tenant_id: str, actor: Actor) -> None:
    """403 when the Run lives in a different tenant than the actor."""
    if run_tenant_id != actor.tenant_id:
        raise HTTPException(status_code=403, detail="Cross-tenant access denied")
