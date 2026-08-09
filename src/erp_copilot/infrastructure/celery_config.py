"""Celery configuration built from application Settings."""

from __future__ import annotations

from erp_copilot.infrastructure.config import Settings


def build_celery_config(settings: Settings) -> dict:
    """Return a Celery configuration dict from application settings."""
    return {
        "broker_url": settings.redis_url,
        "result_backend": settings.redis_url,
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "enable_utc": True,
        "task_track_started": True,
        "worker_hijack_root_logger": False,
    }
