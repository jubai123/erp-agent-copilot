"""Tests for knowledge search API routes (Phase 3.11)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


class TestKnowledgeSearch:
    def test_search_returns_200(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "如何创建订单", "tenant_id": "test-tenant"},
        )
        assert response.status_code == 200

    def test_search_response_structure(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "如何创建订单", "tenant_id": "test-tenant"},
        )
        body = response.json()
        assert "results" in body
        assert "citations" in body
        assert isinstance(body["results"], list)

    def test_missing_query_returns_422(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"tenant_id": "test"},
        )
        assert response.status_code == 422

    def test_missing_tenant_id_returns_422(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "test"},
        )
        assert response.status_code == 422

    def test_empty_query_returns_422(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "", "tenant_id": "test-tenant"},
        )
        assert response.status_code == 422

    def test_empty_tenant_id_returns_422(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "test", "tenant_id": ""},
        )
        assert response.status_code == 422

    def test_custom_top_k(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "test", "tenant_id": "t1", "top_k": 3},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["results"]) <= 3

    def test_top_k_exceeds_max_returns_422(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "test", "tenant_id": "t1", "top_k": 999},
        )
        assert response.status_code == 422
