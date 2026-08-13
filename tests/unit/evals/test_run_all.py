"""Unit tests for the real five-category runners — task 6.6.

Runs the actual run_all.py wiring against the real 175-case datasets. No LLM,
no network: tool_retrieval uses the deterministic candidate_filter, security
uses the real guards with faked DNS, planning runs the real classify →
build_plan → validate_plan node chain with the worker's tool schemas, and
recover_or_replan runs the real recover_or_replan node (hard-wired in
build_agent_graph) on each seeded AgentState snapshot.
knowledge_rag is a real retrieval runner that needs a pgvector database, so
the unit suite injects a fake for that category (the real runner is exercised
by tests/integration/test_retrieval_pipeline.py and
`uv run evals/run_all.py --pipeline`).
"""

from __future__ import annotations

import pytest

from evals.harness import RunnerFn, RunnerOutput, run_all
from evals.run_all import CATEGORY_RUNNERS, _tool_retrieval

EXPECTED_COUNTS = {
    "tool_retrieval": 40,
    "knowledge_rag": 40,
    "planning": 50,
    "recover_or_replan": 20,
    "security": 25,
}


def _fake_knowledge_rag(cases: list[dict]) -> RunnerOutput:
    """Offline stand-in for the DB-backed retrieval runner."""
    return {
        "mode": "injected",
        "primary_score": 1.0,
        "metrics": {"injected": True},
        "per_case": [
            {
                "case_id": c["case_id"],
                "passed": True,
                "expected": None,
                "actual": None,
                "detail": "",
            }
            for c in cases
        ],
    }


@pytest.fixture()
def runners() -> dict[str, RunnerFn]:
    """CATEGORY_RUNNERS with a DB-free knowledge_rag substitution."""
    return {**CATEGORY_RUNNERS, "knowledge_rag": _fake_knowledge_rag}


class TestRealRunners:
    def test_run_all_covers_175(self, runners: dict[str, RunnerFn]) -> None:
        report = run_all(runners)
        assert report["summary"]["total_cases"] == sum(EXPECTED_COUNTS.values())
        assert report["summary"]["categories"] == 5
        assert report["errors"] == {}

    def test_per_category_counts(self, runners: dict[str, RunnerFn]) -> None:
        report = run_all(runners)
        for category, expected in EXPECTED_COUNTS.items():
            assert report["categories"][category]["num_cases"] == expected, category

    def test_scores_are_bounded(self, runners: dict[str, RunnerFn]) -> None:
        report = run_all(runners)
        for category, result in report["categories"].items():
            assert 0.0 <= result["primary_score"] <= 1.0, category

    def test_tool_retrieval_filter_recalls_every_expected_tool(
        self, runners: dict[str, RunnerFn]
    ) -> None:
        report = run_all(runners)
        result = report["categories"]["tool_retrieval"]
        assert result["mode"] == "deterministic"
        # DOMAIN_TOOL_MAP's first-level filter recovers the expected tool for
        # every authored case — the (domain, action) taxonomy suffices at 9 tools.
        assert result["primary_score"] == 1.0
        assert all(pc["passed"] for pc in result["per_case"])

    def test_security_runner_matches_guard_summary(self, runners: dict[str, RunnerFn]) -> None:
        report = run_all(runners)
        result = report["categories"]["security"]
        assert result["mode"] == "deterministic"
        assert result["primary_score"] == 1.0
        assert result["metrics"]["interception_rate"] == 1.0
        assert result["metrics"]["false_positive_rate"] == 0.0

    def test_planning_runs_real_node_chain(self, runners: dict[str, RunnerFn]) -> None:
        report = run_all(runners)
        result = report["categories"]["planning"]
        assert result["mode"] == "deterministic"
        # The real classify → plan → validate node chain passes every case:
        # tool sequences, parameter coverage, structural validation and the
        # stamped WRITE idempotency keys all hold. The key metric guards
        # against a regression that silently drops the stamp step.
        assert result["primary_score"] == 1.0
        assert result["metrics"]["multi_step_count"] == 30
        assert result["metrics"]["write_steps_with_idempotency_key"] > 0
        assert all(pc["passed"] for pc in result["per_case"])

    def test_recover_or_replan_runs_real_node(self, runners: dict[str, RunnerFn]) -> None:
        report = run_all(runners)
        result = report["categories"]["recover_or_replan"]
        assert result["mode"] == "deterministic"
        # The real recover_or_replan node (hard-wired in build_agent_graph)
        # returns the dataset's expected terminal state for every seeded case.
        assert result["primary_score"] == 1.0
        assert result["metrics"]["decision_accuracy"] == 1.0
        assert all(pc["passed"] for pc in result["per_case"])


def _tool_case(**overrides: object) -> dict:
    base: dict = {
        "case_id": "tool-ut",
        "query": "q",
        "domain": "product",
        "action": "query",
        "expected_tool": "getProductByName",
        "candidate_tools": ["getProductByName", "getProductById", "getProductSubstitutesByName"],
        "hard_negative": True,
        "confusion_tool": "createOrder",
    }
    base.update(overrides)
    return base


class TestToolRetrievalHardNegative:
    def test_mislabeled_trap_fails(self) -> None:
        """A trap whose confusion_tool equals its expected_tool is data error —
        the runner must fail it rather than accept a meaningless hard negative."""
        case = _tool_case(expected_tool="getProductByName", confusion_tool="getProductByName")
        out = _tool_retrieval([case])
        assert not out["per_case"][0]["passed"]

    def test_distinct_confusion_passes(self) -> None:
        """Correct routing with a distinct confusion tool passes even when the
        confusion tool is co-surfaced (a legitimate V6 co-candidate)."""
        case = _tool_case(
            domain="order",
            action="cancel",
            expected_tool="cancelOrder",
            confusion_tool="createOrder",
        )
        out = _tool_retrieval([case])
        assert out["per_case"][0]["passed"]

    def test_expected_tool_missing_still_fails(self) -> None:
        case = _tool_case(domain="order", action="create", expected_tool="cancelOrder")
        out = _tool_retrieval([case])
        assert not out["per_case"][0]["passed"]

    def test_confusion_surfaced_metric_counts_co_candidates(self) -> None:
        """order/cancel's filter includes createOrder, so a cancel trap pointing
        at createOrder is counted in confusion_surfaced without failing."""
        case = _tool_case(
            domain="order",
            action="cancel",
            expected_tool="cancelOrder",
            confusion_tool="createOrder",
        )
        out = _tool_retrieval([case])
        assert out["metrics"]["confusion_surfaced_count"] == 1
        assert out["metrics"]["confusion_surfaced_rate"] == 1.0
