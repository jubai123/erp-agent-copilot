"""Unit tests for Prometheus metrics — task 6.4.

The module exposes named collectors for run lifecycle counts, per-phase
latency, and worker queue length, plus a text-format serializer for
``GET /metrics``. No network is touched: every test builds its own fresh
:class:`Metrics` via ``create_metrics()`` so counters never leak between
tests (prometheus_client raises on duplicate registration in one registry).
"""

from __future__ import annotations

from erp_copilot.observability.metrics import (
    METRICS,
    Metrics,
    create_metrics,
    generate_latest,
    set_worker_queue_length,
)

# prometheus_client normalizes Counter family names by stripping the
# "_total" suffix (samples still carry the full "_total" name).
_METRIC_NAMES = {
    "erp_runs_created",
    "erp_runs_completed",
    "erp_runs_failed",
    "erp_phase_latency_seconds",
    "erp_worker_queue_length",
}


def _family_names(metrics: Metrics) -> set[str]:
    return {family.name for family in metrics.registry.collect()}


def _value_lines(text: str, metric: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(metric)]


class TestCreateMetrics:
    def test_registers_all_five_collectors(self) -> None:
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
        metrics.runs_completed.inc()
        assert _value_lines(generate_latest(metrics), "erp_runs_created_total") == [
            "erp_runs_created_total 2.0"
        ]
        assert _value_lines(generate_latest(metrics), "erp_runs_completed_total") == [
            "erp_runs_completed_total 1.0"
        ]
        assert _value_lines(generate_latest(metrics), "erp_runs_failed_total") == [
            "erp_runs_failed_total 0.0"
        ]

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
