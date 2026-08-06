"""Tests for ToolRegistry - tool registration and version management."""

from __future__ import annotations

import pytest
from sqlalchemy import text

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


class TestRegisterNewTool:
    """Acceptance: Register a new tool creates Tool + version 1."""

    def test_register_new_tool_creates_tool_and_version(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-registry")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()
        tool = registry.register(
            name="create_order",
            description="Create a sales order",
            tenant_id=tenant.id,
        )

        assert tool.id is not None
        assert tool.name == "create_order"
        assert tool.description == "Create a sales order"
        assert tool.tenant_id == tenant.id
        assert len(tool.versions) == 1
        assert tool.versions[0].version == 1

    def test_register_new_tool_with_parameters(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-params-reg")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()
        tool = registry.register(
            name="create_order",
            description="Create a sales order",
            tenant_id=tenant.id,
            parameters=[
                {"name": "customer_name", "type": "string", "required": True},
                {"name": "amount", "type": "number", "required": True},
                {"name": "notes", "type": "string", "required": False, "default": ""},
            ],
        )

        v1 = tool.versions[0]
        assert len(v1.parameters) == 3
        assert v1.parameters[0].name == "customer_name"
        assert v1.parameters[0].param_type == "string"
        assert v1.parameters[0].required is True
        assert v1.parameters[2].default_value == ""


class TestGetByName:
    """Acceptance: Query tool by name returns latest version."""

    def test_get_by_name_returns_latest_version(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-getbyname")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()

        # Register two versions of the same tool
        registry.register(name="create_order", description="v1 desc", tenant_id=tenant.id)
        registry.register(name="create_order", description="v2 desc", tenant_id=tenant.id)

        tool = registry.get_by_name("create_order", tenant_id=tenant.id)

        assert tool is not None
        assert tool.name == "create_order"
        assert len(tool.versions) == 2
        # Latest version is the last registered
        max_version = max(v.version for v in tool.versions)
        assert max_version == 2

    def test_get_by_name_nonexistent_returns_none(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-none")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()
        result = registry.get_by_name("nonexistent", tenant_id=tenant.id)

        assert result is None


class TestGetVersion:
    """Acceptance: Query specific tool version by tool_id and version number."""

    def test_get_version_returns_specific_version(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-getver")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()
        tool = registry.register(
            name="create_order",
            description="v1 desc",
            tenant_id=tenant.id,
            risk_level="READ",
        )
        registry.register(
            name="create_order",
            description="v2 desc",
            tenant_id=tenant.id,
            risk_level="WRITE",
        )

        v1 = registry.get_version(tool.id, version=1)
        v2 = registry.get_version(tool.id, version=2)

        assert v1 is not None
        assert v1.version == 1
        assert v1.risk_level == "READ"
        assert v2 is not None
        assert v2.version == 2
        assert v2.risk_level == "WRITE"

    def test_get_version_nonexistent_returns_none(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-nover")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()
        tool = registry.register(name="create_order", tenant_id=tenant.id)

        result = registry.get_version(tool.id, version=999)

        assert result is None


class TestVersionAutoIncrement:
    """Acceptance: Registering same tool name auto-creates next version."""

    def test_same_name_creates_new_version(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-autoinc")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()

        tool1 = registry.register(name="create_order", tenant_id=tenant.id)
        tool2 = registry.register(name="create_order", tenant_id=tenant.id)

        # Same tool instance (same id)
        assert tool1.id == tool2.id
        # But now has two versions
        assert len(tool2.versions) == 2
        assert [v.version for v in tool2.versions] == [1, 2]

    def test_old_versions_preserved(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-preserve")
        session.add(tenant)
        session.commit()

        registry = ToolRegistry()
        registry.register(
            name="create_order",
            tenant_id=tenant.id,
            parameters=[{"name": "old_param", "type": "string", "required": True}],
        )
        registry.register(
            name="create_order",
            tenant_id=tenant.id,
            parameters=[{"name": "new_param", "type": "number", "required": False}],
        )

        tool = registry.get_by_name("create_order", tenant_id=tenant.id)
        assert tool is not None

        # v1 still has old parameters
        v1 = registry.get_version(tool.id, version=1)
        assert v1 is not None
        assert v1.parameters[0].name == "old_param"

        # v2 has new parameters
        v2 = registry.get_version(tool.id, version=2)
        assert v2 is not None
        assert v2.parameters[0].name == "new_param"


class TestTenantIsolation:
    """Acceptance: Tools from different tenants are isolated."""

    def test_same_name_different_tenants_are_separate(self) -> None:
        from erp_copilot.domain.entities import Tenant
        from erp_copilot.tools.registry import ToolRegistry

        session = get_session()
        tenant_a = Tenant(name="Acme", slug="acme-iso-a")
        tenant_b = Tenant(name="Beta", slug="acme-iso-b")
        session.add_all([tenant_a, tenant_b])
        session.commit()

        registry = ToolRegistry()
        tool_a = registry.register(name="create_order", tenant_id=tenant_a.id)
        tool_b = registry.register(name="create_order", tenant_id=tenant_b.id)

        assert tool_a.id != tool_b.id
        assert tool_a.tenant_id == tenant_a.id
        assert tool_b.tenant_id == tenant_b.id

        # Each tenant sees only their own tool
        result_a = registry.get_by_name("create_order", tenant_id=tenant_a.id)
        result_b = registry.get_by_name("create_order", tenant_id=tenant_b.id)
        assert result_a is not None
        assert result_b is not None
        assert result_a.id != result_b.id
