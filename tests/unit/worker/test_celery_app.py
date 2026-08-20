"""Celery worker entry-point observability wiring test — task 6.1/6.2.

A real ``celery -A apps.worker.celery_app worker`` launch must point each forked
child process at JSON logging and a tracer provider, mirroring the API's
lifespan wiring (session 19). The ``worker_process_init`` signal only fires
inside a real worker child, so this test invokes the handler directly and
asserts the setup calls receive Settings-derived arguments — testing the wiring,
not the setup implementation.
"""

from __future__ import annotations

import os
from unittest.mock import Mock

# Settings() (triggered by apps.worker.celery_app at import) requires
# DATABASE_URL. The env file already provides one; this backstop only keeps the
# import working when the env file is absent (same pattern as test_execute_run.py).
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import pytest  # noqa: E402

import apps.worker.celery_app as worker_celery  # noqa: E402
from erp_copilot.infrastructure.celery_config import build_celery_config  # noqa: E402
from erp_copilot.infrastructure.config import Settings  # noqa: E402


class TestCeleryConfig:
    def test_imports_auto_register_worker_tasks(self) -> None:
        # Without `imports`, `celery -A apps.worker.celery_app worker` starts
        # with an EMPTY task registry — execute_run.delay() then fails with
        # KeyError when the worker tries to deserialize the task (observed in
        # the compose stack). Pinning the modules here lets Celery auto-register
        # every @celery_app.task in apps/worker/tasks.py and
        # apps/worker/vocabulary_tasks.py at startup.
        assert build_celery_config(Settings())["imports"] == [
            "apps.worker.tasks",
            "apps.worker.vocabulary_tasks",
        ]


class TestWorkerObservabilityWiring:
    def test_worker_process_init_wires_logging_and_tracing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_celery, "setup_logging", Mock())
        monkeypatch.setattr(worker_celery, "setup_tracing", Mock())
        monkeypatch.setattr(worker_celery, "build_otlp_exporter", Mock(return_value="OTLP"))

        worker_celery._setup_worker_observability()

        worker_celery.setup_logging.assert_called_once_with(
            level="INFO", service="erp-agent-copilot"
        )
        worker_celery.build_otlp_exporter.assert_called_once_with("http://localhost:4317")
        worker_celery.setup_tracing.assert_called_once_with(
            service_name="erp-agent-copilot", exporter="OTLP"
        )

    def test_worker_init_starts_metrics_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # worker_init fires once in the worker controller (parent) before the
        # pool forks; the metrics HTTP server must come up there so Prometheus
        # can scrape the worker's aggregated counters. Assert the wiring only —
        # the actual server is exercised in the docker compose stack.
        monkeypatch.setattr(worker_celery, "start_worker_metrics_server", Mock())

        worker_celery._start_metrics_server()

        worker_celery.start_worker_metrics_server.assert_called_once_with(port=8003)
