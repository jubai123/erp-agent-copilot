"""Unit tests for the run_events error-code taxonomy — task 7.6.

The pure aggregation (no database) is what these pin: given RUN_FAILED event
rows, the error-code distribution and its Top-N ordering must be correct, and
non-failure events must be ignored. The DB-backed ``main`` is exercised only
in the live eval harness.
"""

from __future__ import annotations

from erp_copilot.domain.entities import RunEvent
from evals.scripts.error_taxonomy import aggregate_error_codes, top_n


def _event(event_type: str, payload: str) -> RunEvent:
    return RunEvent(event_type=event_type, payload=payload)


def test_aggregates_error_codes_only_from_run_failed_events() -> None:
    events = [
        _event("RUN_FAILED", '{"error_code": "TIMEOUT"}'),
        _event("RUN_FAILED", '{"error_code": "TIMEOUT"}'),
        _event("RUN_FAILED", '{"error_code": "DEADLINE_EXCEEDED"}'),
        _event("RUN_STATUS", '{"status": "failed"}'),
        _event("RUN_FAILED", '{"failure_reason": "no error_code field"}'),
        _event("RUN_FAILED", '{"error_code": ""}'),
    ]
    assert aggregate_error_codes(events) == {"TIMEOUT": 2, "DEADLINE_EXCEEDED": 1}


def test_top_n_orders_by_frequency_descending() -> None:
    counts = {"DEADLINE_EXCEEDED": 1, "TIMEOUT": 3, "EMPTY_PLAN": 2}
    assert top_n(counts, n=2) == [("TIMEOUT", 3), ("EMPTY_PLAN", 2)]
    assert top_n(counts, n=10) == [
        ("TIMEOUT", 3),
        ("EMPTY_PLAN", 2),
        ("DEADLINE_EXCEEDED", 1),
    ]


def test_top_n_handles_empty_distribution() -> None:
    assert top_n({}, n=5) == []
