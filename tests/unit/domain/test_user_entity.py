"""Tests for multi-tenant user, role, and scope models."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db


@pytest.fixture(scope="module")
def _init_db(ensure_test_database: str) -> None:
    import erp_copilot.domain.entities  # noqa: F401 - register models on Base

    settings = Settings(
        database_url=ensure_test_database,
        llm_api_key="sk-test",
    )
    init_db(settings)
    Base.metadata.create_all(get_engine())


@pytest.fixture(autouse=True)
def _clean_tables(_init_db: None) -> None:
    session = get_session()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(text(f"DELETE FROM {table.name} CASCADE"))
    session.commit()


class TestTenantAndUser:
    """Acceptance: Tenant-User FK relationship, same email across tenants."""

    def test_create_tenant_and_user(self) -> None:
        from erp_copilot.domain.entities import Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme Corp", slug="acme-corp")
        session.add(tenant)
        session.flush()

        user = User(
            tenant_id=tenant.id,
            email="alice@acme.com",
            display_name="Alice",
            hashed_password="hashed",
        )
        session.add(user)
        session.commit()

        assert user.id is not None
        assert user.tenant_id == tenant.id
        assert user.tenant == tenant

    def test_same_email_different_tenants(self) -> None:
        from erp_copilot.domain.entities import Tenant, User

        session = get_session()

        t1 = Tenant(name="Acme", slug="acme")
        t2 = Tenant(name="Globex", slug="globex")
        session.add_all([t1, t2])
        session.flush()

        u1 = User(tenant_id=t1.id, email="alice@example.com", hashed_password="x")
        u2 = User(tenant_id=t2.id, email="alice@example.com", hashed_password="x")
        session.add_all([u1, u2])
        session.commit()

        assert u1.id != u2.id
        assert u1.email == u2.email
        assert u1.tenant_id == t1.id
        assert u2.tenant_id == t2.id

    def test_duplicate_email_same_tenant_raises(self) -> None:
        from erp_copilot.domain.entities import Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-dupe")
        session.add(tenant)
        session.flush()

        session.add(User(tenant_id=tenant.id, email="bob@test.com", hashed_password="x"))
        session.commit()

        with pytest.raises(IntegrityError):
            session.add(User(tenant_id=tenant.id, email="bob@test.com", hashed_password="x"))
            session.commit()

    def test_cascade_delete_tenant_deletes_users(self) -> None:
        from erp_copilot.domain.entities import Tenant, User

        session = get_session()

        tenant = Tenant(name="Temp", slug="temp")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="temp@test.com", hashed_password="x")
        session.add(user)
        session.commit()

        session.delete(tenant)
        session.commit()

        # User should be cascade-deleted
        remaining = session.query(User).filter_by(id=user.id).first()
        assert remaining is None


class TestRoleAndScope:
    """Acceptance: Role-Scope many-to-many via association table."""

    def test_role_many_to_many_scopes(self) -> None:
        from erp_copilot.domain.entities import Role, RoleScope, Tenant

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-m2m")
        session.add(tenant)
        session.flush()

        role = Role(tenant_id=tenant.id, name="admin", description="Administrator")
        session.add(role)
        session.flush()

        scope_tools = RoleScope(role_id=role.id, resource="tools", action="write")
        scope_runs = RoleScope(role_id=role.id, resource="runs", action="read")
        scope_users = RoleScope(role_id=role.id, resource="users", action="manage")
        session.add_all([scope_tools, scope_runs, scope_users])
        session.commit()

        assert len(role.scopes) == 3
        assert scope_tools in role.scopes
        assert scope_runs in role.scopes

    def test_scope_belongs_to_role(self) -> None:
        from erp_copilot.domain.entities import Role, RoleScope, Tenant

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-scope")
        session.add(tenant)
        session.flush()

        role = Role(tenant_id=tenant.id, name="viewer")
        session.add(role)
        session.flush()

        scope = RoleScope(role_id=role.id, resource="dashboards", action="read")
        session.add(scope)
        session.commit()

        assert scope.role_id == role.id
        assert scope.role == role
        assert scope.resource == "dashboards"


class TestUserRole:
    """Users can have multiple roles."""

    def test_user_has_many_roles(self) -> None:
        from erp_copilot.domain.entities import (
            Role,
            Tenant,
            User,
            UserRole,
        )

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-ur")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="multi@test.com", hashed_password="x")
        r1 = Role(tenant_id=tenant.id, name="admin")
        r2 = Role(tenant_id=tenant.id, name="viewer")
        session.add_all([user, r1, r2])
        session.flush()

        session.add(UserRole(user_id=user.id, role_id=r1.id))
        session.add(UserRole(user_id=user.id, role_id=r2.id))
        session.commit()

        assert len(user.roles) == 2
        assert r1 in user.roles
        assert r2 in user.roles
