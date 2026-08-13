"""E2E tests: API -> Celery -> ERP Simulator chain."""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_copilot.domain.entities import (
    Role,
    RoleScope,
    Tenant,
    User,
    UserRole,
)
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


def _make_reader_user(tenant_id: str) -> str:
    """Create an active user holding the READ scopes the planner stamps on read
    steps. DB-driven RBAC (docs/06 §4) resolves scopes from the role graph, so a
    run without a user_id resolves to no scopes and every step is policy-denied.
    """
    session = get_session()
    try:
        role = Role(tenant_id=tenant_id, name="reader")
        session.add(role)
        session.flush()
        for resource, action in [("product", "read"), ("supplier", "read")]:
            session.add(RoleScope(role_id=role.id, resource=resource, action=action))
        user = User(
            tenant_id=tenant_id,
            email="reader@example.com",
            hashed_password="x",
            is_active=True,
        )
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
        return user.id
    finally:
        session.close()


class TestHappyPath:
    """A submitted product-query run reaches COMPLETED with the correct result."""

    def test_query_product_succeeds(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("E2E Happy", "e2e-happy")
        user_id = _make_reader_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={
                "tenant_id": tenant_id,
                "title": "Query product 苹果",
                "product_name": "苹果",
            },
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "COMPLETED"
        assert data["run_id"]

        get_resp = client.get(f"/v1/runs/{data['run_id']}", headers={"X-Tenant-ID": tenant_id})
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == "COMPLETED"

    def test_query_default_product_when_not_specified(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("E2E Default", "e2e-default")
        user_id = _make_reader_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id},
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "COMPLETED"


class TestTimeoutScenario:
    """Under the timeout scenario, the run is marked FAILED."""

    def test_timeout_scenario_marks_run_failed(self) -> None:
        import apps.erp_simulator.scenarios as _scenarios
        from apps.api.main import create_app

        _scenarios._current_scenario = "timeout"

        tenant_id = _create_tenant("E2E Timeout", "e2e-timeout")
        user_id = _make_reader_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={
                "tenant_id": tenant_id,
                "title": "Should timeout",
            },
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "FAILED"

        get_resp = client.get(f"/v1/runs/{data['run_id']}", headers={"X-Tenant-ID": tenant_id})
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == "FAILED"


class TestStockInsufficientScenario:
    """Under stock_insufficient, the task still completes but stock is 0."""

    def test_stock_insufficient_does_not_fail_run(self) -> None:
        import apps.erp_simulator.scenarios as _scenarios
        from apps.api.main import create_app

        _scenarios._current_scenario = "stock_insufficient"

        tenant_id = _create_tenant("E2E Stock", "e2e-stock")
        user_id = _make_reader_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={
                "tenant_id": tenant_id,
                "title": "Check stock",
                "product_name": "苹果",
            },
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "COMPLETED"
