"""Unit tests for security/rbac.resolve_user_scopes — DB-driven scope resolution.

The worker previously granted every run a hard-coded writer scope set
(resolve_worker_scopes). The resolver replaces that placeholder: given
(tenant_id, user_id) it walks User -> roles -> role_scopes from the database and
returns the union as ``resource:action`` strings (order:write, product:read...).
Unknown or inactive users resolve to the empty set — the safe default.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from erp_copilot.domain.entities import Role, RoleScope, Tenant, User, UserRole
from erp_copilot.security.rbac import resolve_user_scopes


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Tenant.__table__.create(engine)
    User.__table__.create(engine)
    Role.__table__.create(engine)
    RoleScope.__table__.create(engine)
    UserRole.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def _make_tenant(session: Session) -> Tenant:
    tenant = Tenant(name="rbac-test", slug="rbac-test")
    session.add(tenant)
    session.commit()
    return tenant


def _make_user(session: Session, tenant: Tenant, *, active: bool = True) -> User:
    user = User(
        tenant_id=tenant.id,
        email="alice@example.com",
        hashed_password="x",
        is_active=active,
    )
    session.add(user)
    session.commit()
    return user


def _make_role(session: Session, tenant: Tenant, name: str, scopes: list[tuple[str, str]]) -> Role:
    role = Role(tenant_id=tenant.id, name=name)
    session.add(role)
    session.flush()
    for resource, action in scopes:
        session.add(RoleScope(role_id=role.id, resource=resource, action=action))
    session.commit()
    return role


def _bind(session: Session, user: User, role: Role) -> None:
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()


class TestResolveUserScopes:
    def test_single_role_scopes_are_resolved(self, session: Session) -> None:
        tenant = _make_tenant(session)
        role = _make_role(session, tenant, "viewer", [("product", "read")])
        user = _make_user(session, tenant)
        _bind(session, user, role)

        assert resolve_user_scopes(session, tenant.id, user.id) == {"product:read"}

    def test_multiple_roles_union_scopes(self, session: Session) -> None:
        tenant = _make_tenant(session)
        reader = _make_role(session, tenant, "reader", [("product", "read"), ("supplier", "read")])
        writer = _make_role(session, tenant, "writer", [("order", "write")])
        user = _make_user(session, tenant)
        _bind(session, user, reader)
        _bind(session, user, writer)

        assert resolve_user_scopes(session, tenant.id, user.id) == {
            "product:read",
            "supplier:read",
            "order:write",
        }

    def test_unknown_user_returns_empty(self, session: Session) -> None:
        assert resolve_user_scopes(session, "ghost-tenant", "ghost-user") == set()

    def test_inactive_user_returns_empty(self, session: Session) -> None:
        tenant = _make_tenant(session)
        user = _make_user(session, tenant, active=False)

        assert resolve_user_scopes(session, tenant.id, user.id) == set()

    def test_role_without_scopes_returns_empty(self, session: Session) -> None:
        tenant = _make_tenant(session)
        role = _make_role(session, tenant, "empty", [])
        user = _make_user(session, tenant)
        _bind(session, user, role)

        assert resolve_user_scopes(session, tenant.id, user.id) == set()

    def test_cross_tenant_scopes_are_isolated(self, session: Session) -> None:
        tenant_a = _make_tenant(session)
        tenant_b = Tenant(name="rbac-b", slug="rbac-b")
        session.add(tenant_b)
        session.commit()
        role = _make_role(session, tenant_a, "writer", [("order", "write")])
        user = _make_user(session, tenant_a)
        _bind(session, user, role)

        # The same user id in the other tenant holds nothing.
        assert resolve_user_scopes(session, tenant_a.id, user.id) == {"order:write"}
        assert resolve_user_scopes(session, tenant_b.id, user.id) == set()
