"""Celery application instance for async task processing."""

from __future__ import annotations

from celery import Celery

from erp_copilot.infrastructure.celery_config import build_celery_config
from erp_copilot.infrastructure.config import Settings


def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    settings = Settings()  # type: ignore[call-arg]
    app = Celery("erp_copilot")
    app.config_from_object(build_celery_config(settings))
    return app


celery_app = create_celery_app()
