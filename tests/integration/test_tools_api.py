"""Integration tests for tool import and listing API endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_copilot.domain.entities import Tenant
from erp_copilot.infrastructure.database import get_session


def _create_tenant(name: str, slug: str) -> str:
    """Create a tenant in the database and return its ID."""
    session = get_session()
    try:
        tenant = Tenant(name=name, slug=slug)
        session.add(tenant)
        session.commit()
        return str(tenant.id)
    finally:
        session.close()


MINIMAL_SPEC: dict = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "1.0"},
    "paths": {
        "/items": {
            "get": {
                "operationId": "getItems",
                "summary": "List all items",
                "parameters": [
                    {"name": "q", "in": "query", "schema": {"type": "string"}},
                ],
            }
        }
    },
}


class TestImportOpenAPI:
    """Acceptance: POST /v1/tools/import/openapi imports tools from a spec."""

    def test_import_valid_spec(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": _create_tenant("Import Test", "import-test")},
        )

        assert response.status_code == 200
        data = response.json()
        assert "imported" in data
        assert len(data["imported"]) == 1
        tool = data["imported"][0]
        assert tool["name"] == "getItems"
        assert tool["current_version"] == 1
        assert tool["risk_level"] == "READ"

    def test_import_invalid_spec_returns_422(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": {"not": "openapi"}, "tenant_id": "t-import-002"},
        )

        assert response.status_code == 422

    def test_import_missing_tenant_id_returns_422(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": ""},
        )

        assert response.status_code == 422
        assert "tenant_id" in response.json()["detail"].lower()


class TestListTools:
    """Acceptance: GET /v1/tools lists tools for a tenant."""

    def test_list_tools_returns_imported_tools(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        # Import first
        tenant_id = _create_tenant("List Test", "list-test")
        client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": tenant_id},
        )

        response = client.get(f"/v1/tools?tenant_id={tenant_id}")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        tool = data[0]
        assert "name" in tool
        assert "current_version" in tool
        assert "risk_level" in tool

    def test_list_tools_empty_tenant(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        tenant_id = _create_tenant("Empty Test", "empty-test")
        response = client.get(f"/v1/tools?tenant_id={tenant_id}")

        assert response.status_code == 200
        data = response.json()
        assert data == []
