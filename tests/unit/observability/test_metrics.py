"""Unit tests for Prometheus metrics — task 6.4.

The module exposes named collectors for run lifecycle counts, per-phase
latency, and worker queue length, plus a text-format serializer for
``GET /metrics``. No network is touched: every test builds its own fresh
:class:`Metrics` via ``create_metrics()`` so counters never leak between
tests (prometheus_client raises on duplicate registration in one registry).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import prometheus_client
import pytest

from erp_copilot.observability.metrics import (
    METRICS,
    Metrics,
    create_metrics,
    create_worker_metrics_registry,
    generate_latest,
    set_worker_queue_length,
)

# prometheus_client normalizes Counter family names by stripping the
# "_total" suffix (samples still carry the full "_total" name).
_METRIC_NAMES = {
    "erp_runs_created",
    "erp_runs_completed",
    "erp_runs_failed",
    "erp_run_retries",
    "erp_run_replans",
    "erp_run_abandoned",
    "erp_approval_requests",
    "erp_plan_outcome",
    "erp_answer_grounded",
    "erp_phase_latency_seconds",
    "erp_worker_queue_length",
}


def _family_names(metrics: Metrics) -> set[str]:
    return {family.name for family in metrics.registry.collect()}


def _value_lines(text: str, metric: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(metric)]


class TestCreateMetrics:
    def test_registers_all_collectors(self) -> None:
        metrics = create_metrics()
        assert _family_names(metrics) == _METRIC_NAMES

    def test_two_registries_are_isolated(self) -> None:
        first, second = create_metrics(), create_metrics()
        first.runs_created.inc()
        assert "erp_runs_created_total 1.0" not in generate_latest(second)

    def test_generate_latest_defaults_to_singleton(self) -> None:
        text = generate_latest()
        assert text.startswith("# HELP erp_runs_created_total")


class TestCounters:
    def test_increment_appears_in_output(self) -> None:
        metrics = create_metrics()
        metrics.runs_created.inc()
        metrics.runs_created.inc()
        metrics.runs_completed.labels(tier="tier1").inc()
        assert _value_lines(generate_latest(metrics), "erp_runs_created_total") == [
            "erp_runs_created_total 2.0"
        ]
        assert _value_lines(generate_latest(metrics), 'erp_runs_completed_total{tier="tier1"}') == [
            'erp_runs_completed_total{tier="tier1"} 1.0'
        ]
        assert _value_lines(generate_latest(metrics), "erp_runs_failed_total") == []

    def test_tier_label_buckets_separate_per_tier(self) -> None:
        metrics = create_metrics()
        metrics.runs_completed.labels(tier="tier1").inc()
        metrics.runs_completed.labels(tier="tier1").inc()
        metrics.runs_completed.labels(tier="tier3").inc()
        metrics.runs_failed.labels(tier="tier2").inc()
        text = generate_latest(metrics)
        assert _value_lines(text, 'erp_runs_completed_total{tier="tier1"}') == [
            'erp_runs_completed_total{tier="tier1"} 2.0'
        ]
        assert _value_lines(text, 'erp_runs_completed_total{tier="tier3"}') == [
            'erp_runs_completed_total{tier="tier3"} 1.0'
        ]
        assert _value_lines(text, 'erp_runs_failed_total{tier="tier2"}') == [
            'erp_runs_failed_total{tier="tier2"} 1.0'
        ]

    def test_recovery_counters_increment(self) -> None:
        metrics = create_metrics()
        metrics.runs_retries.inc()
        metrics.runs_replans.inc()
        metrics.runs_retries.inc()
        metrics.runs_abandoned.inc()
        text = generate_latest(metrics)
        assert _value_lines(text, "erp_run_retries_total") == ["erp_run_retries_total 2.0"]
        assert _value_lines(text, "erp_run_replans_total") == ["erp_run_replans_total 1.0"]
        assert _value_lines(text, "erp_run_abandoned_total") == ["erp_run_abandoned_total 1.0"]

    def test_approval_outcome_labels_increment(self) -> None:
        metrics = create_metrics()
        metrics.approval_requests.labels(outcome="pending").inc()
        metrics.approval_requests.labels(outcome="pending").inc()
        metrics.approval_requests.labels(outcome="approved").inc()
        metrics.approval_requests.labels(outcome="denied").inc()
        text = generate_latest(metrics)
        assert _value_lines(text, 'erp_approval_requests_total{outcome="approved"}') == [
            'erp_approval_requests_total{outcome="approved"} 1.0'
        ]
        assert _value_lines(text, 'erp_approval_requests_total{outcome="denied"}') == [
            'erp_approval_requests_total{outcome="denied"} 1.0'
        ]
        assert _value_lines(text, 'erp_approval_requests_total{outcome="pending"}') == [
            'erp_approval_requests_total{outcome="pending"} 2.0'
        ]

    def test_plan_outcome_labels_increment(self) -> None:
        metrics = create_metrics()
        metrics.plan_outcomes.labels(outcome="accepted").inc()
        metrics.plan_outcomes.labels(outcome="accepted").inc()
        metrics.plan_outcomes.labels(outcome="edited").inc()
        metrics.plan_outcomes.labels(outcome="rejected").inc()
        text = generate_latest(metrics)
        assert _value_lines(text, 'erp_plan_outcome_total{outcome="accepted"}') == [
            'erp_plan_outcome_total{outcome="accepted"} 2.0'
        ]
        assert _value_lines(text, 'erp_plan_outcome_total{outcome="edited"}') == [
            'erp_plan_outcome_total{outcome="edited"} 1.0'
        ]
        assert _value_lines(text, 'erp_plan_outcome_total{outcome="rejected"}') == [
            'erp_plan_outcome_total{outcome="rejected"} 1.0'
        ]

    def test_answer_grounded_labels_increment(self) -> None:
        metrics = create_metrics()
        metrics.answer_grounded.labels(grounded="yes").inc()
        metrics.answer_grounded.labels(grounded="refused").inc()
        text = generate_latest(metrics)
        assert _value_lines(text, 'erp_answer_grounded_total{grounded="yes"}') == [
            'erp_answer_grounded_total{grounded="yes"} 1.0'
        ]
        assert _value_lines(text, 'erp_answer_grounded_total{grounded="refused"}') == [
            'erp_answer_grounded_total{grounded="refused"} 1.0'
        ]
        assert _value_lines(text, 'erp_answer_grounded_total{grounded="no"}') == []

    def test_output_has_help_and_type_lines(self) -> None:
        text = generate_latest(create_metrics())
        assert "# HELP erp_runs_created_total Total number of runs created" in text
        assert "# TYPE erp_runs_created_total counter" in text
        assert "# TYPE erp_phase_latency_seconds histogram" in text
        assert "# TYPE erp_worker_queue_length gauge" in text


class TestPhaseLatencyHistogram:
    def test_records_observation_per_phase(self) -> None:
        metrics = create_metrics()
        metrics.phase_latency.labels(phase="plan").observe(0.5)
        text = generate_latest(metrics)
        assert 'erp_phase_latency_seconds_count{phase="plan"} 1.0' in text
        assert 'erp_phase_latency_seconds_sum{phase="plan"} 0.5' in text
        # bucket lines sort labels alphabetically: le before phase
        assert 'erp_phase_latency_seconds_bucket{le="0.5",phase="plan"} 1.0' in text
        assert 'erp_phase_latency_seconds_bucket{le="0.1",phase="plan"} 0.0' in text

    def test_phases_are_labeled_independently(self) -> None:
        metrics = create_metrics()
        metrics.phase_latency.labels(phase="plan").observe(0.5)
        metrics.phase_latency.labels(phase="execute").observe(2.0)
        text = generate_latest(metrics)
        assert 'erp_phase_latency_seconds_count{phase="plan"} 1.0' in text
        assert 'erp_phase_latency_seconds_count{phase="execute"} 1.0' in text


class TestWorkerQueueGauge:
    def test_gauge_reflects_set_value(self) -> None:
        metrics = create_metrics()
        metrics.worker_queue.set(3)
        assert "erp_worker_queue_length 3.0" in generate_latest(metrics)

    def test_set_worker_queue_length_helper(self) -> None:
        set_worker_queue_length(5)
        try:
            assert "erp_worker_queue_length 5.0" in generate_latest(METRICS)
        finally:
            set_worker_queue_length(0)

    def test_multiprocess_set_is_visible_to_worker_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # In the production worker, PROMETHEUS_MULTIPROC_DIR is set before
        # prometheus_client is imported, so the singleton gauge is an mmap-backed
        # MmapedValue. set_worker_queue_length then writes the parent's .db file,
        # which create_worker_metrics_registry()'s MultiProcessCollector merges —
        # otherwise the queue-depth value would be set in memory but invisible on
        # /metrics (the gauge's reason for staying 0).
        script = (
            "import os\n"
            f'os.environ["PROMETHEUS_MULTIPROC_DIR"] = r"{tmp_path}"\n'
            "from erp_copilot.observability.metrics import set_worker_queue_length\n"
            "set_worker_queue_length(42)\n"
        )
        subprocess.run([sys.executable, "-c", script], check=True)

        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        text = prometheus_client.generate_latest(create_worker_metrics_registry()).decode()
        assert 'erp_worker_queue_length{pid="' in text
        assert "42.0" in text


class TestWorkerMetricsRegistry:
    def test_falls_back_to_singleton_without_multiprocess_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
        # Local dev worker (single process, no mmap dir) serves the same
        # in-process registry the API exposes — values stay correct, not empty.
        assert create_worker_metrics_registry() is METRICS.registry

    def test_builds_multiprocess_registry_when_env_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        registry = create_worker_metrics_registry()
        assert registry is not METRICS.registry
        # A fresh registry exposing only the merged mmap files: the in-process
        # singleton's platform families are absent because their values live in
        # the worker children's per-pid files, not here.
        names = {family.name for family in registry.collect()}
        assert "erp_runs_completed_total" not in names

    def test_multiprocess_registry_aggregates_other_processes_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The worker's counters are advanced by forked children, each writing
        # to its own pid-named mmap file — but only if PROMETHEUS_MULTIPROC_DIR
        # was set BEFORE the first metric was constructed (prometheus_client
        # picks the value class once at import). A real subprocess reproduces
        # that timing; the in-process MultiProcessCollector must merge its file.
        script = (
            "import os\n"
            f'os.environ["PROMETHEUS_MULTIPROC_DIR"] = r"{tmp_path}"\n'
            "import prometheus_client\n"
            "c = prometheus_client.Counter(\n"
            '    "erp_runs_completed_total", "completed",\n'
            "    registry=prometheus_client.CollectorRegistry(),\n"
            ")\n"
            "c.inc()\n"
            "c.inc()\n"
        )
        subprocess.run([sys.executable, "-c", script], check=True)

        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        text = prometheus_client.generate_latest(create_worker_metrics_registry()).decode()
        assert "erp_runs_completed_total 2.0" in text
