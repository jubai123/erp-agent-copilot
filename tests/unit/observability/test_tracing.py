"""Unit tests for OpenTelemetry tracing — task 6.2.

One run_id must串起 the whole chain (docs/08 §5): HTTP request → worker task →
LangGraph node → retrieval → LLM → MCP call → external tool. This module tests
the building blocks — spans carry run_id/step_id from the 6.1 TraceContext,
LangGraph nodes can be wrapped one-span-per-node, and the W3C traceparent
helpers let a downstream service continue the same trace.

Tests run against an InMemorySpanExporter injected through setup_tracing, so
no span is ever emitted to the console or the network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import erp_copilot.observability.tracing as tracing_mod
from erp_copilot.observability.logging import trace_context
from erp_copilot.observability.tracing import (
    build_otlp_exporter,
    extract_trace_context,
    get_tracer,
    inject_trace_headers,
    node_span,
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


def _exporter() -> tuple[InMemorySpanExporter, dict[str, object]]:
    exporter = InMemorySpanExporter()
    setup_tracing(service_name="svc", exporter=exporter)
    return exporter, {}


class TestSetupTracing:
    def test_configures_service_resource(self) -> None:
        _exporter()
        assert tracing_mod._provider.resource.attributes["service.name"] == "svc"

    def test_get_tracer_returns_service_tracer(self) -> None:
        _exporter()
        assert get_tracer().instrumentation_info.name == "svc"


class TestBuildOtlpExporter:
    def test_builds_grpc_exporter_for_endpoint(self) -> None:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = build_otlp_exporter("http://localhost:4317")

        assert isinstance(exporter, OTLPSpanExporter)

    def test_empty_endpoint_returns_none(self) -> None:
        # Empty endpoint means "no remote collector" — returning None lets
        # setup_tracing fall back to the non-blocking console exporter instead
        # of blocking every request on a gRPC connect to an unreachable host.
        assert build_otlp_exporter("") is None


class TestSpan:
    def test_child_spans_share_the_parent_trace(self) -> None:
        exporter, _ = _exporter()
        with span("parent"), span("child"):
            pass
        by_name = {s.name: s for s in exporter.get_finished_spans()}
        assert by_name["child"].context.trace_id == by_name["parent"].context.trace_id
        assert by_name["child"].parent.span_id == by_name["parent"].context.span_id

    def test_run_id_and_step_id_attributes_from_context(self) -> None:
        exporter, _ = _exporter()
        with trace_context(run_id="run-1", step_id="s2"), span("node"):
            pass
        attrs = exporter.get_finished_spans()[0].attributes
        assert attrs["run_id"] == "run-1"
        assert attrs["step_id"] == "s2"

    def test_run_id_omitted_when_unbound(self) -> None:
        exporter, _ = _exporter()
        with span("node"):
            pass
        assert "run_id" not in exporter.get_finished_spans()[0].attributes

    def test_caller_attributes_are_set(self) -> None:
        exporter, _ = _exporter()
        with span("llm", attributes={"model": "gpt-4o", "tokens": 123}):
            pass
        attrs = exporter.get_finished_spans()[0].attributes
        assert attrs["model"] == "gpt-4o"
        assert attrs["tokens"] == 123

    def test_exception_records_error_and_reraises(self) -> None:
        exporter, _ = _exporter()
        with pytest.raises(RuntimeError, match="boom"), span("explode"):
            raise RuntimeError("boom")
        finished = exporter.get_finished_spans()[0]
        assert finished.status.status_code == trace.StatusCode.ERROR
        assert any(event.name == "exception" for event in finished.events)


class TestNodeSpan:
    def test_wraps_graph_node_as_a_span(self) -> None:
        exporter, _ = _exporter()

        @node_span("classify_intent")
        def node(state: dict[str, str]) -> dict[str, str]:
            return {"intent": "order"}

        with trace_context(run_id="r1"):
            result = node({"query": "hi"})

        assert result == {"intent": "order"}
        finished = exporter.get_finished_spans()[0]
        assert finished.name == "classify_intent"
        assert finished.attributes["erp.node"] == "classify_intent"
        assert finished.attributes["run_id"] == "r1"


class TestPropagation:
    def test_inject_then_extract_continues_the_same_trace(self) -> None:
        exporter, _ = _exporter()
        carrier: dict[str, str] = {}

        with span("upstream"):
            inject_trace_headers(carrier)
            assert "traceparent" in carrier

        # Simulate the downstream service: extract and open a span on the
        # inbound traceparent — it must land in the same trace as a child.
        inbound = extract_trace_context(carrier)
        with span("downstream", context=inbound):
            pass

        by_name = {s.name: s for s in exporter.get_finished_spans()}
        upstream, downstream = by_name["upstream"], by_name["downstream"]
        assert downstream.context.trace_id == upstream.context.trace_id
        assert downstream.parent.span_id == upstream.context.span_id
