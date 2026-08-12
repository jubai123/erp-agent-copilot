"""API trace-context middleware wiring test — task 6.3.

The API entry point must apply :class:`TraceContextMiddleware` so an inbound
request carrying a W3C ``traceparent`` continues that trace into the route
handler. The middleware's own behaviour is covered by
``test_http.py``; this test proves the API app actually registers it,
end-to-end, through the ``/health`` endpoint.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

# create_app() imports the runs router, which pulls in the worker graph builder;
# Settings() is only constructed lazily, but the env backstop keeps the import
# path working when the env file is absent (same pattern as test_celery_app.py).
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import erp_copilot.observability.tracing as tracing_mod  # noqa: E402
from apps.api.main import create_app  # noqa: E402
from erp_copilot.observability.tracing import (  # noqa: E402
    inject_trace_headers,
    setup_tracing,
    span,
)


@pytest.fixture(autouse=True)
def _reset_tracing() -> Iterator[None]:
    """Reset the module-scoped provider between tests."""
    tracing_mod._provider = None
    tracing_mod._service_name = tracing_mod._DEFAULT_SERVICE
    yield
    tracing_mod._provider = None


class TestApiTraceContextMiddleware:
    def test_health_continues_inbound_traceparent(self) -> None:
        exporter = InMemorySpanExporter()
        setup_tracing(service_name="svc", exporter=exporter)
        carrier: dict[str, str] = {}
        with span("upstream"):
            inject_trace_headers(carrier)

        client = TestClient(create_app())
        response = client.get("/health", headers={"traceparent": carrier["traceparent"]})

        assert response.status_code == 200
        upstream = next(s for s in exporter.get_finished_spans() if s.name == "upstream")
        http = next(s for s in exporter.get_finished_spans() if s.name == "http.GET")
        assert http.context.trace_id == upstream.context.trace_id
        assert http.parent.span_id == upstream.context.span_id
