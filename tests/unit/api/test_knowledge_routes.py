"""Tests for knowledge search API routes (Phase 3.11, v1.1 pipeline 接线).

The endpoint wires the real retrieval pipeline runner. Unit tests isolate
the endpoint's orchestration — request validation, provider construction,
schema mapping — by injecting a fake search_knowledge and a fake session;
the real retrieval chain is exercised in the integration suite
(tests/integration/test_retrieval_pipeline.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app


def _fake_result(chunk_id: str = "c1", score: float = 0.9) -> dict:
    return {
        "chunk_id": chunk_id,
        "content": "创建订单需提供商品、数量与供应商",
        "section_path": ["订单流程"],
        "char_count": 16,
        "source": "process-order-creation",
        "score": score,
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with DB and provider construction isolated from the unit env."""
    session = MagicMock()

    monkeypatch.setattr("apps.api.routes.knowledge.get_session", lambda: session)
    monkeypatch.setattr("apps.api.routes.knowledge._build_providers", lambda: (None, None))
    return TestClient(create_app())


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch):
    """Replace the retrieval runner so the endpoint never touches a DB."""

    def _apply(results: list[dict]):
        monkeypatch.setattr(
            "apps.api.routes.knowledge._pipeline_search",
            lambda *a, **k: results,
        )

    return _apply


class TestKnowledgeSearch:
    def test_search_returns_200(self, client, stub_pipeline) -> None:
        stub_pipeline([_fake_result()])
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "如何创建订单", "tenant_id": "test-tenant"},
        )
        assert response.status_code == 200

    def test_search_response_structure(self, client, stub_pipeline) -> None:
        stub_pipeline([_fake_result()])
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "如何创建订单", "tenant_id": "test-tenant"},
        )
        body = response.json()
        assert "results" in body
        assert "citations" in body
        assert isinstance(body["results"], list)

    def test_maps_pipeline_result_to_schema(self, client, stub_pipeline) -> None:
        stub_pipeline([_fake_result(score=0.42)])
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "如何创建订单", "tenant_id": "test-tenant"},
        )
        item = response.json()["results"][0]
        assert item["chunk_id"] == "c1"
        assert item["source"] == "process-order-creation"
        assert item["section_path"] == ["订单流程"]
        assert item["score"] == 0.42

    def test_citations_rendered_from_results(self, client, stub_pipeline) -> None:
        stub_pipeline([_fake_result()])
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "如何创建订单", "tenant_id": "test-tenant"},
        )
        citations = response.json()["citations"]
        assert "参考资料" in citations
        assert "process-order-creation" in citations

    def test_empty_results_yield_empty_citations(self, client, stub_pipeline) -> None:
        stub_pipeline([])
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "汇率是多少", "tenant_id": "test-tenant"},
        )
        body = response.json()
        assert body["results"] == []
        assert body["citations"] == ""

    def test_custom_top_k(self, client, stub_pipeline) -> None:
        stub_pipeline([_fake_result()] * 2)
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "test", "tenant_id": "t1", "top_k": 3},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["results"]) <= 3

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

    def test_top_k_exceeds_max_returns_422(self, client) -> None:
        response = client.post(
            "/v1/knowledge/search",
            json={"query": "test", "tenant_id": "t1", "top_k": 999},
        )
        assert response.status_code == 422
