"""Tests for ToolService — OpenAPI import orchestration with validation."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from erp_copilot.domain.errors import ValidationError
from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db

VALID_SPEC: dict = {
    "openapi": "3.0.3",
    "info": {"title": "Test", "version": "v1"},
    "paths": {
        "/products/{id}": {
            "get": {
                "operationId": "getProduct",
                "summary": "Get product by ID",
                "description": "Returns a product",
                "tags": ["products"],
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "description": "Product ID",
                        "schema": {"type": "integer", "format": "int64"},
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


@pytest.fixture(scope="module")
def _init_db(ensure_test_database: str) -> None:
    import erp_copilot.domain.entities  # noqa: F401

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


class TestValidation:
    """Acceptance: Invalid tool definitions are rejected with clear errors."""

    def test_invalid_param_type_raises(self) -> None:
        from erp_copilot.application.tool_service import ToolService
        from erp_copilot.tools.registry import ToolRegistry

        spec = {
            "openapi": "3.0.3",
            "info": {"title": "T", "version": "v1"},
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "badType",
                        "summary": "Bad",
                        "parameters": [
                            {
                                "name": "x",
                                "in": "query",
                                "schema": {"type": "unknown_type"},
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        service = ToolService(ToolRegistry())
        with pytest.raises(ValidationError, match="unknown_type"):
            service.import_from_openapi(spec, tenant_id="t1")

    def test_missing_operation_id_raises(self) -> None:
        from erp_copilot.application.tool_service import ToolService
        from erp_copilot.tools.registry import ToolRegistry

        spec = {
            "openapi": "3.0.3",
            "info": {"title": "T", "version": "v1"},
            "paths": {
                "/items": {
                    "get": {
                        "summary": "No operationId",
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        service = ToolService(ToolRegistry())
        with pytest.raises(ValidationError, match="operationId"):
            service.import_from_openapi(spec, tenant_id="t1")

    def test_empty_param_name_raises(self) -> None:
        from erp_copilot.application.tool_service import ToolService
        from erp_copilot.tools.registry import ToolRegistry

        spec = {
            "openapi": "3.0.3",
            "info": {"title": "T", "version": "v1"},
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "emptyParam",
                        "summary": "Empty param",
                        "parameters": [{"name": "", "in": "query", "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        service = ToolService(ToolRegistry())
        with pytest.raises(ValidationError, match="empty name"):
            service.import_from_openapi(spec, tenant_id="t1")


class TestImportFlow:
    """Acceptance: Valid tools are persisted to Tool + ToolVersion tables."""

    def test_valid_spec_imports_tool(self) -> None:
        from erp_copilot.domain.entities import Tenant

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-import")
        session.add(tenant)
        session.commit()

        from erp_copilot.application.tool_service import ToolService
        from erp_copilot.tools.registry import ToolRegistry

        service = ToolService(ToolRegistry())
        tools = service.import_from_openapi(VALID_SPEC, tenant_id=tenant.id)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "getProduct"
        assert tool.tenant_id == tenant.id
        assert len(tool.versions) == 1
        assert tool.versions[0].version == 1
        assert len(tool.versions[0].parameters) == 1

        param = tool.versions[0].parameters[0]
        assert param.name == "id"
        assert param.param_type == "integer"

    def test_import_multiple_tools(self) -> None:
        from erp_copilot.domain.entities import Tenant

        session = get_session()
        tenant = Tenant(name="Acme", slug="acme-multi-import")
        session.add(tenant)
        session.commit()

        from erp_copilot.application.tool_service import ToolService
        from erp_copilot.tools.registry import ToolRegistry

        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Multi", "version": "v1"},
            "paths": {
                "/products": {
                    "get": {
                        "operationId": "listProducts",
                        "summary": "List products",
                        "responses": {"200": {"description": "OK"}},
                    }
                },
                "/orders": {
                    "post": {
                        "operationId": "createOrder",
                        "summary": "Create order",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/OrderReq"}
                                }
                            }
                        },
                        "responses": {"200": {"description": "OK"}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "OrderReq": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "quantity": {"type": "integer"},
                        },
                    }
                }
            },
        }
        service = ToolService(ToolRegistry())
        tools = service.import_from_openapi(spec, tenant_id=tenant.id)

        assert len(tools) == 2
        assert {t.name for t in tools} == {"listProducts", "createOrder"}
