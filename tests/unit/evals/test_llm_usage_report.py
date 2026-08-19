"""Unit tests for the LLM usage report from structured logs — task 7.7.

Pins the pure aggregation: parsing one JSON log line into a CallRecord (with
estimated cost), grouping calls by run into RunUsage, and the P50/P95
percentile over per-run call counts. The DB-free functions are what the
CLI report (``main``) builds on; only the log-line contract in
erp_copilot.observability.logging.JsonFormatter matters here.
"""

from __future__ import annotations

import json

import pytest

from evals.scripts.llm_usage_report import (
    CallRecord,
    RunUsage,
    aggregate_by_run,
    format_report,
    parse_log_line,
    percentile,
)


def _log_line(
    run_id: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    event_type: str = "LLM_CALL",
) -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-19T01:00:00+00:00",
            "level": "INFO",
            "service": "erp-agent-copilot",
            "logger": "erp_copilot.observability.langfuse",
            "run_id": run_id,
            "message": "LLM call",
            "extra": {
                "event_type": event_type,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": 10.0,
                "error": None,
            },
        }
    )


class TestParseLogLine:
    def test_parses_llm_call_with_estimated_cost(self) -> None:
        rec = parse_log_line(_log_line("run-1", "gpt-4o", 1000, 1000))
        assert rec is not None
        assert rec.run_id == "run-1"
        assert rec.input_tokens == 1000
        assert rec.output_tokens == 1000
        assert rec.cost_usd == pytest.approx(12.5)

    def test_ignores_non_llm_call_events(self) -> None:
        line = _log_line("run-1", "gpt-4o", 5, 5, event_type="RUN_STATUS")
        assert parse_log_line(line) is None

    def test_ignores_malformed_json(self) -> None:
        assert parse_log_line("not json {") is None

    def test_ignores_non_object_json(self) -> None:
        assert parse_log_line("[1, 2]") is None

    def test_ignores_line_without_run_id(self) -> None:
        line = json.dumps(
            {
                "message": "LLM call",
                "extra": {
                    "event_type": "LLM_CALL",
                    "model": "gpt-4o",
                    "input_tokens": 5,
                    "output_tokens": 5,
                },
            }
        )
        assert parse_log_line(line) is None

    def test_ignores_line_without_token_counts(self) -> None:
        assert parse_log_line(_log_line("run-1", "gpt-4o", None, None)) is None

    def test_cost_is_none_for_unpriced_model(self) -> None:
        rec = parse_log_line(_log_line("run-1", "claude-opus-4-7", 100, 100))
        assert rec is not None
        assert rec.cost_usd is None


class TestAggregateByRun:
    def test_sums_tokens_costs_and_counts_per_run(self) -> None:
        records = [
            CallRecord("run-1", "gpt-4o", 1000, 1000, 12.5, 10.0),
            CallRecord("run-1", "gpt-4o-mini", 100, 200, 0.135, 20.0),
            CallRecord("run-2", "gpt-4o", 2000, 500, 10.0, 30.0),
        ]
        runs = {r.run_id: r for r in aggregate_by_run(records)}
        assert runs["run-1"].call_count == 2
        assert runs["run-1"].input_tokens == 1100
        assert runs["run-1"].output_tokens == 1200
        assert runs["run-1"].cost_usd == pytest.approx(12.635)
        assert runs["run-2"].call_count == 1
        assert runs["run-2"].input_tokens == 2000
        assert runs["run-2"].cost_usd == pytest.approx(10.0)

    def test_run_cost_none_when_any_call_unpriced(self) -> None:
        records = [
            CallRecord("run-1", "gpt-4o", 100, 100, 1.25, 10.0),
            CallRecord("run-1", "claude-opus-4-7", 100, 100, None, 20.0),
        ]
        runs = aggregate_by_run(records)
        assert len(runs) == 1
        assert runs[0].cost_usd is None
        assert runs[0].call_count == 2


class TestPercentile:
    def test_nearest_rank_percentiles(self) -> None:
        values = [1, 2, 2, 3, 10]
        assert percentile(values, 50) == 2
        assert percentile(values, 95) == 10

    def test_empty_values_returns_zero(self) -> None:
        assert percentile([], 50) == 0.0

    def test_single_value(self) -> None:
        assert percentile([7], 50) == 7


class TestFormatReport:
    def test_report_covers_averages_and_percentiles(self) -> None:
        runs = [
            RunUsage("a", 1, 100, 100, 1.25),
            RunUsage("b", 2, 200, 200, 2.5),
            RunUsage("c", 3, 300, 300, 3.75),
        ]
        text = format_report(runs)
        assert "3 runs, 6 LLM calls" in text
        assert "average tokens per run: 400" in text
        assert "average cost per run: $2.5" in text
        assert "calls per run — P50=2.0 P95=3.0" in text

    def test_empty_report(self) -> None:
        assert "0 runs, 0 LLM calls" in format_report([])
