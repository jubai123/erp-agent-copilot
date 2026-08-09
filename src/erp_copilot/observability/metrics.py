"""Prometheus metrics — task 6.4.

Exposes the counters, histogram, and gauge the platform tracks (docs/08):
run lifecycle counts, per-phase latency, and worker queue length. The
``/metrics`` endpoint (apps/api/routes/metrics.py) serializes the
module-level :data:`METRICS` registry in the Prometheus text exposition
format.

P50/P95 are NOT computed in-process: the phase histogram emits raw
``_bucket``/``_sum``/``_count`` samples, and PromQL computes quantiles
server-side via ``histogram_quantile(0.5, ...)`` / ``(0.95, ...)`` in
Grafana — the standard Prometheus pattern. Counters and the gauge are
per-process state; with multiple workers, aggregate with ``sum()``.
"""

from __future__ import annotations

from dataclasses import dataclass

import prometheus_client

# Buckets tuned for agent-phase latencies: sub-second LLM calls up to
# multi-minute tool chains. Adjust as the product's latency profile changes.
_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, float("inf"))


@dataclass(frozen=True)
class Metrics:
    """Collectors for the platform's key indicators, bound to one registry."""

    registry: prometheus_client.CollectorRegistry
    runs_created: prometheus_client.Counter
    runs_completed: prometheus_client.Counter
    runs_failed: prometheus_client.Counter
    phase_latency: prometheus_client.Histogram
    worker_queue: prometheus_client.Gauge


def create_metrics() -> Metrics:
    """Build a fresh registry with every collector bound to it.

    A new registry per call keeps tests isolated (prometheus_client rejects
    duplicate registration in one registry) and lets callers own independent
    metric sets.
    """
    registry = prometheus_client.CollectorRegistry()
    return Metrics(
        registry=registry,
        runs_created=prometheus_client.Counter(
            "erp_runs_created_total",
            "Total number of runs created",
            registry=registry,
        ),
        runs_completed=prometheus_client.Counter(
            "erp_runs_completed_total",
            "Total number of runs completed",
            registry=registry,
        ),
        runs_failed=prometheus_client.Counter(
            "erp_runs_failed_total",
            "Total number of runs failed",
            registry=registry,
        ),
        phase_latency=prometheus_client.Histogram(
            "erp_phase_latency_seconds",
            "Latency of one agent phase (plan/execute/verify)",
            labelnames=["phase"],
            buckets=_LATENCY_BUCKETS,
            registry=registry,
        ),
        worker_queue=prometheus_client.Gauge(
            "erp_worker_queue_length",
            "Current length of the worker task queue",
            registry=registry,
        ),
    )


# Module singleton used by production wiring; tests build their own registry.
METRICS = create_metrics()


def generate_latest(metrics: Metrics = METRICS) -> str:
    """Serialize *metrics* in the Prometheus text exposition format."""
    return prometheus_client.generate_latest(metrics.registry).decode()


def set_worker_queue_length(length: int) -> None:
    """Record the current worker queue depth (wired by the worker app)."""
    METRICS.worker_queue.set(length)
