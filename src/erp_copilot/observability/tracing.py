"""OpenTelemetry tracing — task 6.2.

A thin, deterministic wrapper over the OpenTelemetry SDK so one ``run_id``
stays searchable across every hop of the chain (docs/08 §5): HTTP request →
worker task → LangGraph node → retrieval → LLM call → MCP call → external tool.

* :func:`setup_tracing` configures a module-scoped TracerProvider (tests inject
  an in-memory exporter; the default emits to the console).
* :func:`span` runs a block inside a child span that automatically carries
  ``run_id`` / ``step_id`` pulled from the 6.1 TraceContext.
* :func:`node_span` wraps a LangGraph node function so each invocation is one
  span — the graph wiring applies it per node.
* :func:`inject_trace_headers` / :func:`extract_trace_context` propagate the
  W3C ``traceparent`` across process boundaries so a downstream service
  (MCP gateway, simulator) continues the same trace.

Spans never carry secret values; callers must pass only redacted attributes
(docs/08 §5). No auto-instrumentation is relied on — spans are created where
the code explicitly chooses to trace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from erp_copilot.observability.logging import get_trace_context

_DEFAULT_SERVICE = "erp-agent-copilot"
_service_name = _DEFAULT_SERVICE
_provider: TracerProvider | None = None
_propagator = TraceContextTextMapPropagator()


def setup_tracing(
    *,
    service_name: str = _DEFAULT_SERVICE,
    exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Point this module's tracer at a provider emitting through *exporter*.

    The exporter defaults to the console; tests inject an
    :class:`InMemorySpanExporter`. SimpleSpanProcessor exports synchronously on
    span end, so there is no background thread to leak between tests. The
    provider is module-scoped rather than installed globally, which keeps each
    process's instrumentation independent and tests free of cross-test state.
    """
    global _service_name, _provider
    _service_name = service_name
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter or ConsoleSpanExporter()))
    _provider = provider
    return provider


def build_otlp_exporter(endpoint: str) -> SpanExporter:
    """Build an OTLP gRPC span exporter pointed at *endpoint*.

    The import is lazy so the gRPC dependency is only pulled in when an OTLP
    exporter is actually built (the production entry points); unit tests inject
    an :class:`InMemorySpanExporter` and never pay the gRPC import cost.
    """
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint)


def get_tracer() -> trace.Tracer:
    """Return the service tracer from the configured provider.

    Lazily installs the default console-backed provider when none has been
    configured, so callers can trace without an explicit setup step.
    """
    global _provider
    if _provider is None:
        _provider = setup_tracing()
    return _provider.get_tracer(_service_name)


@contextmanager
def span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    context: Context | None = None,
) -> Iterator[trace.Span]:
    """Run the block inside a child span tagged with the run identity.

    ``run_id`` / ``step_id`` are read from the current TraceContext (task 6.1)
    and attached as span attributes; values that are unbound are omitted (OTel
    rejects None attributes). *context* is an optional inbound propagation
    context that makes this span a child of a remote span (cross-service).
    Exceptions are recorded, the span is marked ERROR, and the exception is
    re-raised.
    """
    ctx = get_trace_context()
    attrs: dict[str, Any] = {}
    if ctx.run_id is not None:
        attrs["run_id"] = ctx.run_id
    if ctx.step_id is not None:
        attrs["step_id"] = ctx.step_id
    if attributes:
        attrs.update(attributes)
    with get_tracer().start_as_current_span(name, attributes=attrs, context=context) as active:
        try:
            yield active
        except Exception as exc:
            active.record_exception(exc)
            active.set_status(trace.Status(trace.StatusCode.ERROR))
            raise


_P = ParamSpec("_P")
_R = TypeVar("_R")


def node_span(node_name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorate a LangGraph node so each invocation becomes one span.

    The span name and the ``erp.node`` attribute both carry *node_name*, and
    the run identity is attached automatically from the TraceContext.
    """

    def decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with span(node_name, attributes={"erp.node": node_name}):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def inject_trace_headers(carrier: dict[str, str]) -> dict[str, str]:
    """Write the current span's ``traceparent`` into *carrier* (outbound)."""
    _propagator.inject(carrier)
    return carrier


def extract_trace_context(carrier: Mapping[str, str]) -> Context:
    """Read an inbound ``traceparent`` so downstream work continues the trace."""
    return _propagator.extract(carrier)
