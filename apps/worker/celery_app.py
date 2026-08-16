"""Celery application instance for async task processing."""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_init, worker_process_init
from redis import Redis

from erp_copilot.infrastructure.celery_config import build_celery_config
from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import init_db
from erp_copilot.observability.logging import setup_logging
from erp_copilot.observability.metrics import (
    start_queue_length_reporter,
    start_worker_metrics_server,
)
from erp_copilot.observability.tracing import build_otlp_exporter, setup_tracing


def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    settings = Settings()  # type: ignore[call-arg]
    app = Celery("erp_copilot")
    app.config_from_object(build_celery_config(settings))
    return app


celery_app = create_celery_app()


@worker_process_init.connect
def _setup_worker_observability(**kwargs: object) -> None:
    """Wire JSON logging and tracing into each forked worker child process.

    Celery's prefork pool forks one child per concurrency slot; the child
    inherits the parent's root logger and module-scoped tracer provider, so each
    process re-initialises its own. ``worker_hijack_root_logger=False`` (from
    build_celery_config) keeps Celery off the root logger, so the JSON handler
    installed here is the one every task log line flows through.
    """
    settings = Settings()  # type: ignore[call-arg]
    init_db(settings)
    setup_logging(level=settings.log_level, service=settings.app_name)
    setup_tracing(
        service_name=settings.app_name,
        exporter=build_otlp_exporter(settings.otel_exporter_endpoint),
    )


@worker_init.connect
def _start_metrics_server(**kwargs: object) -> None:
    """Start the worker's ``/metrics`` scrape endpoint in the parent process.

    ``worker_init`` fires once in the worker controller before the pool forks;
    the threaded WSGI server aggregates the per-pid mmap files the forked
    children write (prometheus multiprocess mode — only when
    ``PROMETHEUS_MULTIPROC_DIR`` is set in the worker's environment). Children
    inherit the listening socket but never accept on it, so the parent keeps
    serving. Without the env var it serves the in-process singleton instead.
    """
    settings = Settings()  # type: ignore[call-arg]
    start_worker_metrics_server(port=settings.worker_metrics_port)

    # Wire the queue-depth gauge in the parent process: sample LLEN on every
    # queue this worker consumes and record it, so /metrics reports the real
    # backlog instead of a permanent 0. Runs here (not in a forked child) so a
    # single process owns the value; in multiprocess mode the parent writes its
    # own mmap file, which the scrape endpoint merges.
    redis_client = Redis.from_url(settings.redis_url)
    queue_names = list(celery_app.amqp.queues.keys())
    start_queue_length_reporter(redis_client, queue_names)
