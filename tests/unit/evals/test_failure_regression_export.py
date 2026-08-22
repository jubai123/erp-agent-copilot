"""Pure-function tests for the P2 failure-regression export script.

The script turns the last N days of FAILED runs (the human-intervention queue)
into eval cases under the failure_20.json schema. The DB-coupled parts
(collect_failed_runs, checkpoint query recovery) are integration-tested in
tests/integration/test_failure_regression_export.py; here we pin the pure
mapping logic: failure-code classification, case building, dataset rendering,
and the recover_query title fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime

from erp_copilot.domain.entities import Run
from evals.scripts.export_failure_regression import (
    build_case,
    classify_failure,
    render_dataset,
)


def _run(**overrides: object) -> Run:
    base: dict[str, object] = {
        "id": "run-1",
        "tenant_id": "tenant-1",
        "title": "",
        "status": "FAILED",
        "failure_code": "TIMEOUT",
        "failure_reason": "request timed out",
        "suggested_action": "检查 ERP Simulator 可用性后重试",
        "completed_at": datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Run(**base)


class TestClassifyFailure:
    def test_maps_known_failure_codes(self) -> None:
        assert classify_failure("TIMEOUT") == ("tool_timeout", "retry")
        assert classify_failure("RECOVERY_RECONCILIATION_REQUIRED") == (
            "reconciliation",
            "reconcile_no_duplicate",
        )
        assert classify_failure("DEADLINE_EXCEEDED") == ("deadline", "fail_fast_notify")
        assert classify_failure("INSUFFICIENT_STOCK") == ("business", "no_failure")

    def test_unknown_code_falls_back(self) -> None:
        assert classify_failure("SOME_NEW_CODE") == ("unknown", "no_failure")

    def test_empty_code_falls_back(self) -> None:
        assert classify_failure("") == ("unknown", "no_failure")


class TestBuildCase:
    def test_builds_case_in_failure_schema(self) -> None:
        run = _run()
        case = build_case(run, "下单 10 KG 苹果到上海", index=3)
        assert case["case_id"] == "reg-003"
        assert case["query"] == "下单 10 KG 苹果到上海"
        assert case["scenario"] == "tool_timeout"
        assert case["expected_behavior"] == "retry"
        # The idempotency invariant is asserted on every regression case.
        assert case["expected_duplicate_writes"] == 0

    def test_note_carries_observed_failure_and_run_id(self) -> None:
        case = build_case(_run(), "q", index=1)
        assert "run-1" in case["note"]
        assert "request timed out" in case["note"]
        assert "检查 ERP Simulator 可用性后重试" in case["note"]

    def test_classify_reads_failure_code_from_run(self) -> None:
        case = build_case(_run(failure_code="WRITE_OUTCOME_AMBIGUOUS"), "q", index=1)
        assert case["scenario"] == "reconciliation"
        assert case["expected_behavior"] == "reconcile_no_duplicate"


class TestRenderDataset:
    def test_wraps_cases_in_dataset_envelope(self) -> None:
        cases = [{"case_id": "reg-001", "query": "q"}]
        dataset = render_dataset(cases, days=7)
        assert dataset["version"] == "1.0"
        assert dataset["cases"] == cases
        assert "最近 7 天" in dataset["description"]

    def test_empty_case_list_is_valid(self) -> None:
        dataset = render_dataset([], days=3)
        assert dataset["cases"] == []
