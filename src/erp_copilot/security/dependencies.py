"""FastAPI dependencies: header-based actor identity + scope/tenant guards.

docs/06 §4 and docs/07 §10: the API must not trust client-submitted tenant,
user, or scope — they come from the authentication context (``X-Tenant-ID`` /
``X-User-ID`` headers) and the role graph in the database. ``get_actor``
resolves the acting user's effective scopes once per request; ``require_scope``
turns a missing scope into a 403; ``require_run_tenant`` rejects cross-tenant
access to a resource. The worker's policy_check node remains the authoritative
plan-time scope gate — these dependencies are the API-boundary fast-fail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from erp_copilot.infrastructure.database import get_session
from erp_copilot.security.rbac import resolve_user_scopes


@dataclass(frozen=True)
class Actor:
    """Authenticated identity for a single request (headers + DB role graph)."""

    tenant_id: str
    user_id: str | None
    scopes: frozenset[str]


def get_actor(
    x_tenant_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> Actor:
    """Resolve the acting user's identity and effective scopes.

    A missing tenant header is unauthenticated (401) — there is no safe default
    tenant to fall back to. A missing user header resolves to an anonymous
    actor with no scopes (system-initiated runs), matching the pre-header
    behaviour where a run without a user id executes as no-scope.
    """
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-ID header")
    session = get_session()
    try:
        scopes = resolve_user_scopes(session, x_tenant_id, x_user_id or "")
    finally:
        session.close()
    return Actor(tenant_id=x_tenant_id, user_id=x_user_id, scopes=frozenset(scopes))


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
