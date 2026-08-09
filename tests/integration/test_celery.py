"""Integration tests for Celery application and task execution."""

from __future__ import annotations

import pytest


class TestCeleryConfig:
    """Acceptance: Celery app reads configuration from Settings."""

    @pytest.fixture
    def celery_app(self):
        from apps.worker.celery_app import create_celery_app

        return create_celery_app()

    def test_broker_url_configured(self, celery_app) -> None:
        conf = celery_app.conf
        assert conf.broker_url is not None
        assert "redis" in conf.broker_url

    def test_result_backend_configured(self, celery_app) -> None:
        conf = celery_app.conf
        assert conf.result_backend is not None
        assert "redis" in conf.result_backend

    def test_json_serializer_configured(self, celery_app) -> None:
        conf = celery_app.conf
        assert conf.task_serializer == "json"
        assert conf.result_serializer == "json"
        assert "json" in conf.accept_content

    def test_timezone_is_utc(self, celery_app) -> None:
        conf = celery_app.conf
        assert conf.timezone == "UTC"


class TestTaskExecution:
    """Acceptance: A simple task can be submitted and its result retrieved."""

    @pytest.fixture
    def celery_app(self):
        from apps.worker.celery_app import create_celery_app

        app = create_celery_app()
        app.conf.task_always_eager = True
        return app

    def test_simple_task_executes(self, celery_app) -> None:
        @celery_app.task(name="test.ping")
        def ping() -> str:
            return "pong"

        result = ping.delay()
        assert result.get(timeout=5) == "pong"

    def test_task_with_arguments(self, celery_app) -> None:
        @celery_app.task(name="test.add")
        def add(x: int, y: int) -> int:
            return x + y

        result = add.delay(3, 4)
        assert result.get(timeout=5) == 7
