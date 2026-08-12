"""Celery application instance for async task processing."""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from erp_copilot.infrastructure.celery_config import build_celery_config
from erp_copilot.infrastructure.config import Settings
from erp_copilot.observability.logging import setup_logging
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
    setup_logging(level=settings.log_level, service=settings.app_name)
    setup_tracing(
        service_name=settings.app_name,
        exporter=build_otlp_exporter(settings.otel_exporter_endpoint),
    )
