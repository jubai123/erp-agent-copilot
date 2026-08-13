"""Integration tests for tool import and listing API endpoints.

The tools endpoints now require an authenticated actor holding the
``admin:tool`` scope (docs/06 §4), and the body/query tenant must match the
header identity (docs/07 §10). Identity comes from X-Tenant-ID / X-User-ID
headers; the body/query tenant is never trusted on its own.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_copilot.domain.entities import Role, RoleScope, Tenant, User, UserRole
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


def _make_user(tenant_id: str, email: str, scopes: list[tuple[str, str]]) -> str:
    """Create an active user holding *scopes* in *tenant_id*; return its id."""
    session = get_session()
    try:
        role = Role(tenant_id=tenant_id, name=f"role-{email}")
        session.add(role)
        session.flush()
        for resource, action in scopes:
            session.add(RoleScope(role_id=role.id, resource=resource, action=action))
        user = User(tenant_id=tenant_id, email=email, hashed_password="x", is_active=True)
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
        return user.id
    finally:
        session.close()


def _headers(tenant_id: str, user_id: str | None = None) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant_id}
    if user_id is not None:
        headers["X-User-ID"] = user_id
    return headers


def _admin_user(tenant_id: str) -> str:
    """Create a user holding the admin:tool scope in *tenant_id*; return its id."""
    return _make_user(tenant_id, f"admin-{tenant_id}@example.com", [("admin", "tool")])


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

        tenant_id = _create_tenant("Import Test", "import-test")
        user_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": tenant_id},
            headers=_headers(tenant_id, user_id),
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

        tenant_id = _create_tenant("Invalid Spec", "invalid-spec")
        user_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": {"not": "openapi"}, "tenant_id": tenant_id},
            headers=_headers(tenant_id, user_id),
        )

        assert response.status_code == 422

    def test_import_missing_tenant_id_returns_422(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Missing Tenant", "missing-tenant")
        user_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": ""},
            headers=_headers(tenant_id, user_id),
        )

        assert response.status_code == 422
        assert "tenant_id" in response.json()["detail"].lower()


class TestImportOpenAPIScope:
    """The import endpoint requires an authenticated admin:tool actor."""

    def test_missing_tenant_header_is_401(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": "t"},
        )

        assert response.status_code == 401

    def test_without_admin_tool_scope_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("No Admin", "no-admin")
        user_id = _make_user(tenant_id, "viewer@example.com", [("product", "read")])
        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": tenant_id},
            headers=_headers(tenant_id, user_id),
        )

        assert response.status_code == 403

    def test_cross_tenant_body_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("Tools A", "tools-a")
        user_a = _admin_user(tenant_a)
        tenant_b = _create_tenant("Tools B", "tools-b")
        client = TestClient(create_app())

        response = client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": tenant_b},
            headers=_headers(tenant_a, user_a),
        )

        assert response.status_code == 403


class TestListTools:
    """Acceptance: GET /v1/tools lists tools for a tenant."""

    def test_list_tools_returns_imported_tools(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("List Test", "list-test")
        user_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        # Import first
        client.post(
            "/v1/tools/import/openapi",
            json={"spec": MINIMAL_SPEC, "tenant_id": tenant_id},
            headers=_headers(tenant_id, user_id),
        )

        response = client.get(
            f"/v1/tools?tenant_id={tenant_id}", headers=_headers(tenant_id, user_id)
        )

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

        tenant_id = _create_tenant("Empty Test", "empty-test")
        user_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.get(
            f"/v1/tools?tenant_id={tenant_id}", headers=_headers(tenant_id, user_id)
        )

        assert response.status_code == 200
        data = response.json()
        assert data == []


class TestListToolsScope:
    """The list endpoint requires an authenticated admin:tool actor."""

    def test_missing_tenant_header_is_401(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.get("/v1/tools?tenant_id=t")

        assert response.status_code == 401

    def test_cross_tenant_query_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("List A", "list-a")
        user_a = _admin_user(tenant_a)
        tenant_b = _create_tenant("List B", "list-b")
        client = TestClient(create_app())

        response = client.get(f"/v1/tools?tenant_id={tenant_b}", headers=_headers(tenant_a, user_a))

        assert response.status_code == 403
