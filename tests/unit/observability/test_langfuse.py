"""Unit tests for LLM observability — task 6.3 (Langfuse-style telemetry).

Every LLM call records input/output token counts, latency, and an estimated
cost onto an OTel span (6.2) plus a structured log line (6.1), both tagged
with run_id so a run's LLM call chain is filterable in one query. No LLM is
invoked and no network is touched — the context manager only measures the
surrounding block and the caller supplies token usage.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import erp_copilot.observability.tracing as tracing_mod
from erp_copilot.observability.langfuse import estimate_cost, llm_call
from erp_copilot.observability.logging import trace_context
from erp_copilot.observability.tracing import setup_tracing

_LOGGER_NAME = "erp_copilot.observability.langfuse"


@pytest.fixture(autouse=True)
def _reset_tracing() -> Iterator[None]:
    tracing_mod._provider = None
    tracing_mod._service_name = tracing_mod._DEFAULT_SERVICE
    yield
    tracing_mod._provider = None


def _setup() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    setup_tracing(service_name="svc", exporter=exporter)
    return exporter


class TestEstimateCost:
    def test_known_model_computes_from_tokens(self) -> None:
        # gpt-4o: $2.50 / 1K input, $10.00 / 1K output
        assert estimate_cost("gpt-4o", 1000, 1000) == pytest.approx(12.5)

    def test_scales_with_token_count(self) -> None:
        assert estimate_cost("gpt-4o-mini", 2000, 3000) == pytest.approx(
            (2000 / 1000) * 0.15 + (3000 / 1000) * 0.60
        )

    def test_unknown_model_returns_none(self) -> None:
        assert estimate_cost("claude-4", 1000, 1000) is None

    def test_missing_tokens_returns_none(self) -> None:
        assert estimate_cost("gpt-4o", None, 1000) is None


class TestLlmCall:
    def test_records_tokens_latency_and_cost_on_span(self) -> None:
        exporter = _setup()
        with llm_call(model="gpt-4o", prompt="create an order") as call:
            call.completion = "ok"
            call.input_tokens = 1000
            call.output_tokens = 1000
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert spans[0].name == "llm.gpt-4o"
        assert attrs["model"] == "gpt-4o"
        assert attrs["input_tokens"] == 1000
        assert attrs["output_tokens"] == 1000
        assert attrs["total_tokens"] == 2000
        assert attrs["latency_ms"] >= 0
        assert attrs["estimated_cost_usd"] == pytest.approx(12.5)

    def test_run_id_carried_on_span(self) -> None:
        exporter = _setup()
        with trace_context(run_id="run-9"), llm_call(model="gpt-4o-mini", prompt="hi") as call:
            call.input_tokens = 5
            call.output_tokens = 5
        assert exporter.get_finished_spans()[0].attributes["run_id"] == "run-9"

    def test_llm_call_chain_in_one_run(self) -> None:
        exporter = _setup()
        with trace_context(run_id="run-7"):
            with llm_call(model="gpt-4o", prompt="p1") as c1:
                c1.input_tokens = 10
                c1.output_tokens = 10
            with llm_call(model="gpt-4o-mini", prompt="p2") as c2:
                c2.input_tokens = 20
                c2.output_tokens = 20
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert {s.attributes["model"] for s in spans} == {"gpt-4o", "gpt-4o-mini"}
        assert all(s.attributes["run_id"] == "run-7" for s in spans)

    def test_tokens_omitted_when_not_supplied(self) -> None:
        exporter = _setup()
        with llm_call(model="gpt-4o", prompt="hi"):
            pass
        attrs = exporter.get_finished_spans()[0].attributes
        assert "input_tokens" not in attrs
        assert "estimated_cost_usd" not in attrs

    def test_exception_marks_error_and_reraises(self) -> None:
        exporter = _setup()
        with (
            pytest.raises(RuntimeError, match="llm failed"),
            llm_call(model="gpt-4o", prompt="boom"),
        ):
            raise RuntimeError("llm failed")
        finished = exporter.get_finished_spans()[0]
        assert finished.status.status_code == trace.StatusCode.ERROR
        assert finished.attributes["error"] == "llm failed"

    def test_emits_structured_log_with_tokens(self, caplog: pytest.LogCaptureFixture) -> None:
        _setup()
        caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
        with llm_call(model="gpt-4o", prompt="hello") as call:
            call.input_tokens = 5
            call.output_tokens = 7
        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert len(records) == 1
        assert records[0].event_type == "LLM_CALL"
        assert records[0].model == "gpt-4o"
        assert records[0].input_tokens == 5
        assert records[0].output_tokens == 7
