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

import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass

import prometheus_client
from prometheus_client.multiprocess import MultiProcessCollector
from redis import Redis

logger = logging.getLogger(__name__)

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
    runs_retries: prometheus_client.Counter
    runs_replans: prometheus_client.Counter
    runs_abandoned: prometheus_client.Counter
    approval_requests: prometheus_client.Counter
    plan_outcomes: prometheus_client.Counter
    answer_grounded: prometheus_client.Counter
    reconciliation_success: prometheus_client.Counter
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
            "Total number of runs completed, labeled by funnel tier",
            labelnames=["tier"],
            registry=registry,
        ),
        runs_failed=prometheus_client.Counter(
            "erp_runs_failed_total",
            "Total number of runs failed, labeled by funnel tier",
            labelnames=["tier"],
            registry=registry,
        ),
        runs_retries=prometheus_client.Counter(
            "erp_run_retries_total",
            "Total number of step retries (recover_or_replan retry branch)",
            registry=registry,
        ),
        runs_replans=prometheus_client.Counter(
            "erp_run_replans_total",
            "Total number of plan regenerations (recover_or_replan replan branch)",
            registry=registry,
        ),
        runs_abandoned=prometheus_client.Counter(
            "erp_run_abandoned_total",
            "Total number of runs abandoned to FAILED (recover_or_replan give-up branch)",
            registry=registry,
        ),
        approval_requests=prometheus_client.Counter(
            "erp_approval_requests_total",
            "Total number of human approval requests, labeled by outcome",
            labelnames=["outcome"],
            registry=registry,
        ),
        plan_outcomes=prometheus_client.Counter(
            "erp_plan_outcome_total",
            "Total number of user decisions on an AI plan, labeled by outcome",
            labelnames=["outcome"],
            registry=registry,
        ),
        answer_grounded=prometheus_client.Counter(
            "erp_answer_grounded_total",
            "Total number of terminal answers, labeled by groundedness verdict",
            labelnames=["grounded"],
            registry=registry,
        ),
        reconciliation_success=prometheus_client.Counter(
            "erp_reconciliation_success_total",
            "Total number of write-path reconciliations, labeled by verdict",
            labelnames=["outcome"],
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


def create_worker_metrics_registry() -> prometheus_client.CollectorRegistry:
    """Registry for the worker's ``/metrics`` scrape endpoint.

    Celery's prefork pool forks one child per concurrency slot; every child
    holds a private copy of :data:`METRICS`, so the counters they advance (via
    :func:`run_persistence.persist_run` and the graph's phase wrapper) live in
    per-pid mmap files — but only when ``PROMETHEUS_MULTIPROC_DIR`` was set
    before the first metric was constructed (prometheus_client picks its value
    class once, at import). :class:`MultiProcessCollector` merges every ``*.db``
    file into the response. Without the env var — e.g. a solo-pool local dev
    worker — fall back to the in-process singleton so the endpoint stays
    correct instead of empty.
    """
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ or "prometheus_multiproc_dir" in os.environ:
        registry = prometheus_client.CollectorRegistry()
        MultiProcessCollector(registry=registry)
        return registry
    return METRICS.registry


def start_worker_metrics_server(port: int) -> None:
    """Serve the worker's aggregated metrics on *port* in a background thread.

    Starts a threaded WSGI server (``start_http_server``) using
    :func:`create_worker_metrics_registry`, so the worker parent can be scraped
    by Prometheus without a second uvicorn process.
    """
    prometheus_client.start_http_server(port=port, registry=create_worker_metrics_registry())


def sample_queue_length(redis_client: Redis, queue_names: Iterable[str]) -> int:
    """Sum the pending-message count across *queue_names* in the Redis broker.

    Celery stores each queue as a Redis list, so ``LLEN`` is the depth of tasks
    waiting to be consumed — the real backlog. This deliberately excludes
    Celery's ``active``/``reserved`` inspect counters, which describe tasks the
    workers have already claimed rather than tasks still queued.
    """
    total = 0
    for name in queue_names:
        # redis-py types llen as int | Awaitable[int] (shared with its async
        # client); the sync call always returns int, and an absent key is 0.
        length = redis_client.llen(name)
        if isinstance(length, int):
            total += length
    return total


def refresh_queue_length(redis_client: Redis, queue_names: Iterable[str]) -> int:
    """Sample broker depth and record it on the worker_queue gauge.

    A broker that is down or slow must never take down the reporter thread (or
    the worker it lives in), so sampling failures degrade to 0 after logging.
    Returns the value recorded on the gauge.
    """
    try:
        length = sample_queue_length(redis_client, queue_names)
    except Exception as exc:
        logger.warning("queue-depth sampling failed, recording 0: %s", exc)
        length = 0
    METRICS.worker_queue.set(length)
    return length


def start_queue_length_reporter(
    redis_client: Redis,
    queue_names: Iterable[str],
    interval_s: float = 5.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Start a daemon thread that periodically records broker queue depth.

    Runs in the worker parent (from ``worker_init``), not a forked child, so a
    single process owns the gauge. In multiprocess mode the parent's value is
    written to its own mmap file and merged into ``/metrics`` by
    :class:`MultiProcessCollector`; without the env var it serves the in-process
    singleton instead. A daemon thread needs no explicit shutdown: it dies with
    the worker process.
    """
    event = stop_event or threading.Event()

    def _loop() -> None:
        while not event.is_set():
            refresh_queue_length(redis_client, queue_names)
            event.wait(interval_s)

    thread = threading.Thread(
        target=_loop,
        name="queue-length-reporter",
        daemon=True,
    )
    thread.start()
    return thread
