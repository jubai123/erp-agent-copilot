"""Integration tests for header-based identity + scope enforcement (task 5.1).

Identity comes from X-Tenant-ID / X-User-ID headers, not the body. create_run
pre-checks the required scope (classify_intent -> resource:action) and rejects
with 403; the cross-tenant guard rejects access to a Run in a different tenant.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_copilot.domain.entities import Role, RoleScope, Tenant, User, UserRole
from erp_copilot.infrastructure.database import get_session


def _create_tenant(name: str, slug: str) -> str:
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


class TestCreateRunScope:
    """POST /v1/runs requires a tenant header and the query's required scope."""

    def test_missing_tenant_header_is_401(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post("/v1/runs", json={"tenant_id": "t", "title": "x"})

        assert response.status_code == 401

    def test_write_query_without_order_write_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("No Write", "no-write")
        reader_id = _make_user(
            tenant_id, "reader@example.com", [("product", "read"), ("supplier", "read")]
        )
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "下单", "query": "帮我在上海下一单 1 KG 苹果"},
            headers=_headers(tenant_id, reader_id),
        )

        assert response.status_code == 403

    def test_write_query_with_order_write_is_202(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Has Write", "has-write")
        writer_id = _make_user(
            tenant_id,
            "writer@example.com",
            [("product", "read"), ("supplier", "read"), ("order", "write")],
        )
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "下单", "query": "帮我在上海下一单 1 KG 苹果"},
            headers=_headers(tenant_id, writer_id),
        )

        assert response.status_code == 202
        assert response.json()["status"] == "WAITING_APPROVAL"

    def test_supplier_read_query_with_supplier_scope_is_202(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Supplier Read", "supplier-read")
        user_id = _make_user(tenant_id, "s@example.com", [("supplier", "read")])
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "供应商", "query": "上海有哪些供应商"},
            headers=_headers(tenant_id, user_id),
        )

        assert response.status_code == 202

    def test_body_tenant_mismatching_header_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("Body A", "body-a")
        tenant_b = _create_tenant("Body B", "body-b")
        user_b = _make_user(tenant_b, "b@example.com", [("supplier", "read")])
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_a, "title": "x", "query": "上海有哪些供应商"},
            headers=_headers(tenant_b, user_b),
        )

        assert response.status_code == 403


class TestCrossTenantGuard:
    """Run rows never cross the actor's tenant boundary."""

    def test_cross_tenant_get_run_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("Cross A", "cross-a")
        user_a = _make_user(tenant_a, "a@example.com", [("product", "read")])
        tenant_b = _create_tenant("Cross B", "cross-b")
        user_b = _make_user(tenant_b, "b@example.com", [("product", "read")])
        client = TestClient(create_app())

        create_resp = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_a, "title": "mine"},
            headers=_headers(tenant_a, user_a),
        )
        assert create_resp.status_code == 202
        run_id = create_resp.json()["run_id"]

        ok = client.get(f"/v1/runs/{run_id}", headers=_headers(tenant_a, user_a))
        assert ok.status_code == 200

        forbidden = client.get(f"/v1/runs/{run_id}", headers=_headers(tenant_b, user_b))
        assert forbidden.status_code == 403
