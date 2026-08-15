"""Config-drift guards for the Prometheus + Grafana wiring (infra/).

The observability stack is pure configuration — a prometheus.yml scrape
config, a Grafana datasource, and a checked-in dashboard. None of it is
executable Python, so instead of a runtime test these guards parse the files
as they would be consumed and assert the wiring that a misconfigured stack
would silently break: the scrape targets must match the compose service names,
the datasource must point at the Prometheus service, and the dashboard panels
must reference the metric names emitted by metrics.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INFRA = _REPO_ROOT / "infra"


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestPrometheusScrapeConfig:
    def test_scrape_configs_cover_the_metrics_exposing_service(self) -> None:
        cfg = _load_yaml(_INFRA / "prometheus" / "prometheus.yml")
        targets = {
            target
            for job in cfg["scrape_configs"]
            for target in job["static_configs"][0]["targets"]
        }
        # Only the API process registers the metrics router today; the worker is
        # covered in a follow-up task once it gains a metrics endpoint.
        assert "api:8000" in targets

    def test_does_not_scrape_services_without_metrics(self) -> None:
        cfg = _load_yaml(_INFRA / "prometheus" / "prometheus.yml")
        targets = {
            target
            for job in cfg["scrape_configs"]
            for target in job["static_configs"][0]["targets"]
        }
        # erp_simulator and mcp_gateway return 404 on /metrics — scraping them
        # would make a healthy service report up=0 (a false "down").
        assert "mcp_gateway:8002" not in targets
        assert "erp_simulator:8001" not in targets

    def test_every_job_scrapes_the_metrics_path(self) -> None:
        cfg = _load_yaml(_INFRA / "prometheus" / "prometheus.yml")
        for job in cfg["scrape_configs"]:
            assert job.get("metrics_path", "/metrics") == "/metrics"


class TestGrafanaDatasource:
    def test_datasource_points_at_prometheus_service(self) -> None:
        ds = _load_yaml(_INFRA / "grafana" / "provisioning" / "datasources" / "prometheus.yml")
        sources = ds["datasources"]
        assert len(sources) == 1
        assert sources[0]["type"] == "prometheus"
        assert sources[0]["url"] == "http://prometheus:9090"
        assert sources[0]["isDefault"] is True

    def test_dashboard_provider_points_at_mounted_dir(self) -> None:
        provider = _load_yaml(
            _INFRA / "grafana" / "provisioning" / "dashboards" / "dashboard_provider.yml"
        )
        assert provider["providers"][0]["type"] == "file"
        assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"


class TestDashboardJson:
    @pytest.fixture
    def dashboard(self) -> dict:
        raw = (_INFRA / "grafana" / "dashboards" / "erp_metrics.json").read_text(encoding="utf-8")
        return json.loads(raw)

    def test_panels_reference_the_platform_metrics(self, dashboard: dict) -> None:
        text = json.dumps(dashboard)
        # Every metric emitted by observability/metrics.py should appear in the
        # dashboard so a renamed/removed metric fails loudly instead of showing
        # an empty panel.
        for metric in (
            "erp_runs_created_total",
            "erp_runs_completed_total",
            "erp_runs_failed_total",
            "erp_phase_latency_seconds",
            "erp_worker_queue_length",
        ):
            assert metric in text, f"dashboard must reference {metric}"
