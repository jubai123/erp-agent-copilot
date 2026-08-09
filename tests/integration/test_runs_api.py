"""Integration tests for Run creation and query endpoints."""

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


class TestCreateRun:
    """Acceptance: POST /v1/runs creates a new Run."""

    def test_create_run_returns_202_and_run_id(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Run Test", "run-test")
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "Test run"},
        )

        assert response.status_code == 202
        data = response.json()
        assert "run_id" in data
        assert data["status"] == "COMPLETED"

    def test_create_run_default_title(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Run Default", "run-default")
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["run_id"]
        assert data["status"] == "COMPLETED"


class TestGetRun:
    """Acceptance: GET /v1/runs/{run_id} returns Run status."""

    def test_get_existing_run(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Get Run", "get-run")
        client = TestClient(create_app())

        create_resp = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "My run"},
        )
        run_id = create_resp.json()["run_id"]

        response = client.get(f"/v1/runs/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == run_id
        assert data["status"] == "COMPLETED"
        assert data["title"] == "My run"

    def test_get_nonexistent_run_returns_404(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.get("/v1/runs/nonexistent-id")

        assert response.status_code == 404
        assert "detail" in response.json()
