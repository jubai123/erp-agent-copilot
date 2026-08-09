"""Unit tests for the real six-category runners — task 6.6.

Runs the actual run_all.py wiring against the real 200-case datasets. No LLM,
no DB, no network: tool_retrieval uses the deterministic candidate_filter,
security uses the real guards with faked DNS, and the agent-runtime categories
run a golden baseline oracle (see evals/run_all.py for the mode labels).
"""

from __future__ import annotations

from evals.harness import run_all
from evals.run_all import CATEGORY_RUNNERS

EXPECTED_COUNTS = {
    "tool_retrieval": 40,
    "knowledge_rag": 40,
    "planning": 50,
    "recovery": 25,
    "security": 25,
    "failure": 20,
}


class TestRealRunners:
    def test_run_all_covers_200(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        assert report["summary"]["total_cases"] == 200
        assert report["summary"]["categories"] == 6
        assert report["errors"] == {}

    def test_per_category_counts(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        for category, expected in EXPECTED_COUNTS.items():
            assert report["categories"][category]["num_cases"] == expected, category

    def test_scores_are_bounded(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        for category, result in report["categories"].items():
            assert 0.0 <= result["primary_score"] <= 1.0, category

    def test_tool_retrieval_filter_recalls_every_expected_tool(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        result = report["categories"]["tool_retrieval"]
        assert result["mode"] == "deterministic"
        # DOMAIN_TOOL_MAP's first-level filter recovers the expected tool for
        # every authored case — the (domain, action) taxonomy suffices at 9 tools.
        assert result["primary_score"] == 1.0
        assert all(pc["passed"] for pc in result["per_case"])

    def test_security_runner_matches_guard_summary(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        result = report["categories"]["security"]
        assert result["mode"] == "deterministic"
        assert result["primary_score"] == 1.0
        assert result["metrics"]["interception_rate"] == 1.0
        assert result["metrics"]["false_positive_rate"] == 0.0

    def test_planning_golden_baseline(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        result = report["categories"]["planning"]
        assert result["mode"] == "golden_baseline"
        assert result["primary_score"] == 1.0
        assert result["metrics"]["multi_step_count"] == 30

    def test_failure_golden_baseline_no_duplicate_violations(self) -> None:
        report = run_all(CATEGORY_RUNNERS)
        result = report["categories"]["failure"]
        assert result["mode"] == "golden_baseline"
        assert result["primary_score"] == 1.0
        assert result["metrics"]["duplicate_write_violations"] == 0
