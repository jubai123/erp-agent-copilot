"""Per-run LLM usage report from structured logs — task 7.7.

Every LLM invocation writes one JSON ``LLM_CALL`` log line (task 6.1/6.3):
``run_id`` sits at the top level, the call's model/token counts/latency in
``extra`` (see erp_copilot.observability.logging.JsonFormatter). Those lines
are a queryable data warehouse on their own — no extra instrumentation needed.
This script reads a log file, groups the calls by run, and reports average
tokens and cost per run plus the call-count distribution (P50/P95).

Cost is NOT in the log line; it is re-estimated here from the same price table
via ``erp_copilot.observability.langfuse.estimate_cost``. A run whose calls mix
priced and unpriced models reports no total cost (an honest "cannot estimate"),
never a partial figure presented as complete.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from erp_copilot.observability.langfuse import estimate_cost

LLM_CALL = "LLM_CALL"


@dataclass(frozen=True)
class CallRecord:
    """One parsed LLM_CALL log line, normalised for aggregation."""

    run_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: float | None


@dataclass(frozen=True)
class RunUsage:
    """LLM usage summed across one run's calls."""

    run_id: str
    call_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def parse_log_line(line: str) -> CallRecord | None:
    """Parse one JSON log line; None when it is not an aggregatable LLM_CALL.

    Non-JSON lines, non-dict payloads, non-LLM_CALL events, lines without a
    run_id, and calls without token counts are all skipped — each is either not
    a call or cannot be attributed/quantified.
    """
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    extra = payload.get("extra") or {}
    if extra.get("event_type") != LLM_CALL:
        return None
    run_id = payload.get("run_id")
    input_tokens = extra.get("input_tokens")
    output_tokens = extra.get("output_tokens")
    if not run_id or not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    model = extra.get("model")
    return CallRecord(
        run_id=run_id,
        model=model or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=estimate_cost(model, input_tokens, output_tokens),
        latency_ms=extra.get("latency_ms"),
    )


@dataclass
class _Accumulator:
    count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_total: float = 0.0
    cost_unknown: bool = False


def aggregate_by_run(records: Iterable[CallRecord]) -> list[RunUsage]:
    """Group calls by run, summing tokens/cost and counting calls."""
    accumulators: dict[str, _Accumulator] = {}
    for rec in records:
        acc = accumulators.setdefault(rec.run_id, _Accumulator())
        acc.count += 1
        acc.input_tokens += rec.input_tokens
        acc.output_tokens += rec.output_tokens
        if rec.cost_usd is None:
            acc.cost_unknown = True
        else:
            acc.cost_total += rec.cost_usd
    return [
        RunUsage(
            run_id=run_id,
            call_count=acc.count,
            input_tokens=acc.input_tokens,
            output_tokens=acc.output_tokens,
            cost_usd=None if acc.cost_unknown else round(acc.cost_total, 6),
        )
        for run_id, acc in accumulators.items()
    ]


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile of *values* (order-independent), 0.0 when empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(p / 100.0 * len(ordered)) - 1
    rank = max(0, min(rank, len(ordered) - 1))
    return float(ordered[rank])


def format_report(runs: list[RunUsage]) -> str:
    """Human-readable report: per-run averages plus the call-count P50/P95."""
    total_calls = sum(run.call_count for run in runs)
    if not runs:
        return "0 runs, 0 LLM calls"
    average_tokens = sum(run.input_tokens + run.output_tokens for run in runs) / len(runs)
    priced = [run.cost_usd for run in runs if run.cost_usd is not None]
    if priced:
        average_cost = sum(priced) / len(priced)
        cost_line = f"average cost per run: ${average_cost:.1f}"
        if len(priced) < len(runs):
            cost_line += " (unpriced runs excluded)"
    else:
        cost_line = "average cost per run: unavailable (no priced calls)"
    calls = [run.call_count for run in runs]
    return (
        f"{len(runs)} runs, {total_calls} LLM calls\n"
        f"average tokens per run: {average_tokens:.0f}\n"
        f"{cost_line}\n"
        f"calls per run — P50={percentile(calls, 50):.1f} P95={percentile(calls, 95):.1f}"
    )


def main(path: str) -> None:
    """Read *path* (one JSON log line per row) and print the usage report."""
    records: list[CallRecord] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = parse_log_line(line)
            if rec is not None:
                records.append(rec)
    runs = aggregate_by_run(records)
    print(format_report(runs))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Structured JSON log file (one line per record)")
    args = parser.parse_args()
    main(args.path)
