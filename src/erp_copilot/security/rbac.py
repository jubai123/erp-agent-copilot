"""DB-driven role scope resolution — replaces the worker's hard-coded grant.

docs/06 §4: permissions are tenant-scoped and roles only aggregate scopes —
business exceptions must not be hard-coded in the runtime. Given
(tenant_id, user_id) this walks User -> Role -> RoleScope and returns the
union as ``resource:action`` strings (order:write, product:read...), the same
format the deterministic planner stamps into PlanStep.required_scope and the
policy_check node matches against.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from erp_copilot.domain.entities import User


def resolve_user_scopes(session: Session, tenant_id: str, user_id: str) -> set[str]:
    """Resolve a user's effective scopes within a tenant from the role graph.

    Unknown or inactive user -> empty set (safe default: no user, no scopes).
    The user_id never escapes the tenant boundary: the lookup keys on both
    id and tenant_id, so a cross-tenant id resolves nothing.
    """
    user = session.query(User).filter_by(id=user_id, tenant_id=tenant_id, is_active=True).first()
    if user is None:
        return set()
    return {f"{scope.resource}:{scope.action}" for role in user.roles for scope in role.scopes}
