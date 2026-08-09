"""Integration tests for the health check endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    """GET /health returns application status."""

    @pytest.fixture
    def client(self) -> TestClient:
        from apps.api.main import create_app

        app = create_app()
        return TestClient(app)

    def test_returns_200(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200

    def test_returns_json(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.headers["content-type"] == "application/json"

    def test_status_is_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "ok"

    def test_includes_app_name(self, client: TestClient) -> None:
        response = client.get("/health")
        data = response.json()
        assert data["app_name"] == "erp-agent-copilot"

    def test_includes_version(self, client: TestClient) -> None:
        response = client.get("/health")
        data = response.json()
        assert "version" in data
