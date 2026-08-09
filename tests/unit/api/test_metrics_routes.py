"""Tests for the Prometheus /metrics endpoint (task 6.4)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app

_METRIC_NAMES = (
    "erp_runs_created_total",
    "erp_runs_completed_total",
    "erp_runs_failed_total",
    "erp_phase_latency_seconds",
    "erp_worker_queue_length",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class TestMetricsEndpoint:
    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/metrics").status_code == 200

    def test_content_type_is_prometheus_text(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"

    def test_body_is_prometheus_text_format(self, client: TestClient) -> None:
        text = client.get("/metrics").text
        assert text.startswith("# HELP")
        assert "# TYPE erp_runs_created_total counter" in text

    def test_contains_all_expected_metric_families(self, client: TestClient) -> None:
        text = client.get("/metrics").text
        for name in _METRIC_NAMES:
            assert name in text
