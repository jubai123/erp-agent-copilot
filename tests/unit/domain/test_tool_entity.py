"""Tests for Tool, ToolVersion, and Parameter models."""

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


class TestToolAndVersion:
    """Acceptance: Tool-Version 1:N relationship with immutable versions."""

    def test_create_tool(self) -> None:
        from erp_copilot.domain.entities import Tenant, Tool

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-tool")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order", description="Create a sales order")
        session.add(tool)
        session.commit()

        assert tool.id is not None
        assert tool.name == "create_order"
        assert tool.tenant_id == tenant.id

    def test_create_tool_version(self) -> None:
        from erp_copilot.domain.entities import Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-version")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        v1 = ToolVersion(tool_id=tool.id, version=1, risk_level="WRITE")
        session.add(v1)
        session.commit()

        assert v1.id is not None
        assert v1.tool_id == tool.id
        assert v1.version == 1
        assert v1.risk_level == "WRITE"

    def test_tool_has_many_versions(self) -> None:
        from erp_copilot.domain.entities import Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-many-v")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        v1 = ToolVersion(tool_id=tool.id, version=1, risk_level="READ")
        v2 = ToolVersion(tool_id=tool.id, version=2, risk_level="WRITE")
        session.add_all([v1, v2])
        session.commit()

        assert len(tool.versions) == 2
        assert v1 in tool.versions
        assert v2 in tool.versions

    def test_version_immutability_concept(self) -> None:
        """Each change to a tool creates a new version; old versions are preserved."""
        from erp_copilot.domain.entities import Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-immutable")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        v1 = ToolVersion(tool_id=tool.id, version=1, risk_level="READ")
        session.add(v1)
        session.commit()

        # Create a new version instead of modifying v1
        v2 = ToolVersion(tool_id=tool.id, version=2, risk_level="WRITE")
        session.add(v2)
        session.commit()

        # v1 is unchanged
        session.refresh(v1)
        assert v1.risk_level == "READ"
        assert v2.risk_level == "WRITE"
        assert len(tool.versions) == 2


class TestParameter:
    """Acceptance: Parameter supports type, required flag, and default value."""

    def test_create_parameter(self) -> None:
        from erp_copilot.domain.entities import Parameter, Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-param")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        version = ToolVersion(tool_id=tool.id, version=1)
        session.add(version)
        session.flush()

        param = Parameter(
            tool_version_id=version.id,
            name="customer_name",
            param_type="string",
            required=True,
            description="Name of the customer",
        )
        session.add(param)
        session.commit()

        assert param.id is not None
        assert param.name == "customer_name"
        assert param.param_type == "string"
        assert param.required is True
        assert param.default_value is None
        assert param.description == "Name of the customer"

    def test_parameter_with_default_value(self) -> None:
        from erp_copilot.domain.entities import Parameter, Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-default")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        version = ToolVersion(tool_id=tool.id, version=1)
        session.add(version)
        session.flush()

        param = Parameter(
            tool_version_id=version.id,
            name="page_size",
            param_type="integer",
            required=False,
            default_value="10",
        )
        session.add(param)
        session.commit()

        assert param.required is False
        assert param.default_value == "10"

    def test_version_has_many_parameters(self) -> None:
        from erp_copilot.domain.entities import Parameter, Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-params")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        version = ToolVersion(tool_id=tool.id, version=1)
        session.add(version)
        session.flush()

        p1 = Parameter(tool_version_id=version.id, name="customer_name", param_type="string")
        p2 = Parameter(
            tool_version_id=version.id, name="amount", param_type="number", required=True
        )
        session.add_all([p1, p2])
        session.commit()

        assert len(version.parameters) == 2


class TestCascadeDelete:
    """Deleting a tool removes all its versions and their parameters."""

    def test_cascade_delete_tool_removes_versions_and_parameters(self) -> None:
        from erp_copilot.domain.entities import Parameter, Tenant, Tool, ToolVersion

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-cascade-tool")
        session.add(tenant)
        session.flush()

        tool = Tool(tenant_id=tenant.id, name="create_order")
        session.add(tool)
        session.flush()

        version = ToolVersion(tool_id=tool.id, version=1)
        session.add(version)
        session.flush()

        param = Parameter(tool_version_id=version.id, name="x", param_type="string")
        session.add(param)
        session.commit()

        tool_id = tool.id
        version_id = version.id
        param_id = param.id

        session.delete(tool)
        session.commit()

        assert session.get(Tool, tool_id) is None
        assert session.get(ToolVersion, version_id) is None
        assert session.get(Parameter, param_id) is None
