"""Deterministic eval for the real recover_or_replan node — task 6.6 upgrade.

The dataset seeds map each recover_or_replan_20.json case onto an AgentState
snapshot (build_seed_state) and the runner calls recover_or_replan — the node
hard-wired in build_agent_graph — directly, asserting the terminal state
(status, retry/replan budgets, terminal error code, errors cleared). This is
the same node the worker and approve-resume paths run, replacing the
harness-only decide_recovery_action / decide_failure_behavior modules.

Terminal statuses are compared by enum name (e.g. "EXECUTING") to match the
dataset's authoring convention, where seed.status and expected.status both use
the uppercase AgentStatus member names.
"""

from __future__ import annotations

from typing import Any

from erp_copilot.agent.nodes.recover_or_replan import recover_or_replan
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    Plan,
    PlanStep,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from evals.harness import PerCaseResult, RunnerOutput

_SIGNATURE_KEYS = ("status", "retry_count", "replan_count", "error_code", "errors_cleared")


def build_seed_state(seed: dict, *, case_id: str) -> AgentState:
    """Map one dataset seed onto the AgentState snapshot recover_or_replan reads.

    The base state (run_id/tenant_id/query) is the eval-only scaffold; the
    fields the node branches on — status, retry/replan counters, errors, plan,
    step_results — come from the seed. Enum lookups are strict (KeyError on a
    bad member) so a typo in the dataset fails loudly instead of silently
    changing the scenario.
    """
    updates: dict[str, Any] = {
        "status": AgentStatus[seed["status"]],
        "retry_count": int(seed.get("retry_count", 0)),
        "replan_count": int(seed.get("replan_count", 0)),
        "errors": [
            StateError(code=e["code"], message=e["message"], step_id=e.get("step_id"))
            for e in seed.get("errors", [])
        ],
    }
    plan = seed.get("plan")
    if plan is not None:
        updates["plan"] = Plan(
            steps=[
                PlanStep(
                    step_id=s["step_id"],
                    tool_name=s["tool_name"],
                    risk_level=ToolRiskLevel[s["risk_level"]],
                    idempotency_key=s.get("idempotency_key"),
                )
                for s in plan["steps"]
            ]
        )
    updates["step_results"] = {
        sr["step_id"]: StepResult(
            step_id=sr["step_id"],
            status=StepStatus[sr["status"]],
            error_code=sr.get("error_code"),
            error_message=sr.get("error_message"),
            is_retryable=sr.get("is_retryable", False),
        )
        for sr in seed.get("step_results", [])
    }
    return AgentState(run_id=f"eval-{case_id}", tenant_id="eval", query="").model_copy(
        update=updates
    )


def _terminal_signature(state: AgentState) -> dict[str, object]:
    """Project the terminal state onto the dataset's expected signature."""
    return {
        "status": state.status.name,
        "retry_count": state.retry_count,
        "replan_count": state.replan_count,
        # A give-up branch appends its code as the last error; every other
        # branch clears errors, so the terminal error is always the final one.
        "error_code": state.errors[-1].code if state.errors else None,
        "errors_cleared": state.errors == [],
    }


def evaluate_cases(cases: list[dict]) -> RunnerOutput:
    """Run the real recover_or_replan node over every case and score the outcomes."""
    per_case: list[PerCaseResult] = []
    for case in cases:
        state = build_seed_state(case["seed"], case_id=case["case_id"])
        terminal = state.model_copy(update=recover_or_replan(state))
        actual = _terminal_signature(terminal)
        expected = {k: case["expected"][k] for k in _SIGNATURE_KEYS}
        mismatches = [k for k in expected if actual.get(k) != expected[k]]
        passed = not mismatches
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected,
                "actual": actual,
                "detail": (
                    ""
                    if passed
                    else "; ".join(
                        f"{k}: actual {actual[k]!r} != expected {expected[k]!r}" for k in mismatches
                    )
                ),
            }
        )
    n = len(per_case)
    accuracy = sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0
    return {
        "mode": "deterministic",
        "primary_score": accuracy,
        "metrics": {"decision_accuracy": accuracy},
        "per_case": per_case,
    }
