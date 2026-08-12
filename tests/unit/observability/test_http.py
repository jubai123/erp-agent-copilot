"""Unit tests for TraceContextMiddleware — task 6.3.

The middleware must continue an inbound W3C ``traceparent`` rather than start a
fresh trace, so an HTTP request from the API or a worker into the simulator or
gateway lands in the same trace. Tests drive the middleware through a real
Starlette app under TestClient, with an InMemorySpanExporter capturing spans.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

import erp_copilot.observability.tracing as tracing_mod
from erp_copilot.observability.http import TraceContextMiddleware
from erp_copilot.observability.tracing import inject_trace_headers, setup_tracing, span


@pytest.fixture(autouse=True)
def _reset_tracing() -> Iterator[None]:
    """Reset the module-scoped provider between tests."""
    tracing_mod._provider = None
    tracing_mod._service_name = tracing_mod._DEFAULT_SERVICE
    yield
    tracing_mod._provider = None


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    return app


class TestTraceContextMiddleware:
    def test_continues_inbound_traceparent(self) -> None:
        exporter = InMemorySpanExporter()
        setup_tracing(service_name="svc", exporter=exporter)
        carrier: dict[str, str] = {}
        with span("upstream"):
            inject_trace_headers(carrier)

        client = TestClient(_build_app())
        client.get("/ping", headers={"traceparent": carrier["traceparent"]})

        finished = exporter.get_finished_spans()
        upstream = next(s for s in finished if s.name == "upstream")
        http = next(s for s in finished if s.name == "http.GET")
        assert http.context.trace_id == upstream.context.trace_id
        assert http.parent.span_id == upstream.context.span_id

    def test_records_http_attributes(self) -> None:
        exporter = InMemorySpanExporter()
        setup_tracing(service_name="svc", exporter=exporter)

        client = TestClient(_build_app())
        client.get("/ping")

        http = next(s for s in exporter.get_finished_spans() if s.name == "http.GET")
        assert http.attributes["http.method"] == "GET"
        assert http.attributes["http.target"] == "/ping"
