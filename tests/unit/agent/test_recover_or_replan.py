"""Unit tests for the recover_or_replan node — tasks 4.11/5.8.

The graph's single recovery sink: after validate_plan or verify_results reports
errors, this node decides retry (back to execute_ready_steps) vs replan (back
to build_plan) vs give up (finalize -> FAILED), bounded by run-level retry and
replan budgets. Two invariants must hold:

- termination: every continuing decision strictly increments retry_count or
  replan_count, both capped, so any input sequence reaches FAILED/SUCCEEDED;
- at-most-once: a WRITE/DANGEROUS step that failed without an idempotency key
  is never auto-re-run (its outcome is uncertain), and a run annotated with
  RECOVERY_RECONCILIATION_REQUIRED gives up for a human instead.
"""

from __future__ import annotations

from typing import Any

import pytest

import erp_copilot.agent.nodes.recover_or_replan as recover_mod
from erp_copilot.agent.nodes.recover_or_replan import (
    MAX_REPLANS,
    MAX_RETRIES,
    recover_or_replan,
)
from erp_copilot.agent.recovery import RECOVERY_RECONCILIATION_REQUIRED
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    Plan,
    PlanStep,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.observability.metrics import create_metrics, generate_latest


def _step(step_id: str = "s1", **overrides: object) -> PlanStep:
    data: dict[str, object] = {
        "step_id": step_id,
        "tool_name": "getProductById",
        "risk_level": ToolRiskLevel.READ,
        "depends_on": [],
    }
    data.update(overrides)
    return PlanStep(**data)


def _failed(
    step_id: str,
    *,
    is_retryable: bool,
    risk: ToolRiskLevel = ToolRiskLevel.READ,
    error_code: str | None = None,
) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.FAILED,
        error_code=error_code or ("TIMEOUT" if is_retryable else "PERMISSION_DENIED"),
        is_retryable=is_retryable,
    )


def _state(
    *,
    status: AgentStatus = AgentStatus.QUEUED,
    plan: Plan | None = None,
    step_results: dict[str, StepResult] | None = None,
    errors: list[StateError] | None = None,
    retry_count: int = 0,
    replan_count: int = 0,
) -> AgentState:
    return AgentState(
        run_id="r1",
        tenant_id="t1",
        user_id="u1",
        query="q",
        status=status,
        plan=plan,
        step_results=step_results or {},
        errors=errors or [],
        retry_count=retry_count,
        replan_count=replan_count,
    )


def _invoke(state: AgentState) -> dict[str, Any]:
    return recover_or_replan(state)


class TestRetryBranch:
    def test_retryable_failure_retries_once(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True)},
            errors=[StateError(code="TIMEOUT", message="timeout", step_id="s1")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.EXECUTING
        assert updates["retry_count"] == 1
        assert updates["errors"] == []

    def test_completed_steps_survive_a_retry(self) -> None:
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={
                "s1": StepResult(step_id="s1", status=StepStatus.COMPLETED),
                "s2": _failed("s2", is_retryable=True),
            },
            errors=[StateError(code="TIMEOUT", message="timeout", step_id="s2")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.EXECUTING
        # A retry must not touch step_results — the graph merge keeps completed
        # steps so the executor's resume guard skips them on the next pass.
        assert "step_results" not in updates

    def test_retry_budget_exhausted_gives_up(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True)},
            retry_count=MAX_RETRIES,
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "RETRY_BUDGET_EXHAUSTED"

    def test_retry_without_plan_gives_up(self) -> None:
        state = _state(status=AgentStatus.RETRYING)
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "RECOVERY_GIVE_UP"


class TestAmbiguousWriteGate:
    """A FAILED WRITE/DANGEROUS step carrying an ambiguous transient code
    (TIMEOUT/UPSTREAM_UNAVAILABLE/UPSTREAM_5xx) gives up for a human — the cloud
    may have applied the write and has no server-side idempotency, so retry OR
    replan would re-invoke it and risk a duplicate order. The gate sits before
    the status branches so a replan-path ambiguous write never auto-continues
    either. It subsumes the old no-key WRITE_RETRY_UNSAFE guard: every retryable
    write failure in this system is ambiguous, and the cloud drops the key.
    """

    def test_ambiguous_write_without_key_gives_up(self) -> None:
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.WRITE)])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True, risk=ToolRiskLevel.WRITE)},
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "WRITE_OUTCOME_AMBIGUOUS"

    def test_ambiguous_write_with_key_still_gives_up(self) -> None:
        # The cloud ERP drops the idempotency_key, so a keyed write is just as
        # ambiguous on TIMEOUT — this was WRITE_RETRY_UNSAFE's "safe" case and is
        # now also gated.
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.WRITE, idempotency_key="k-1")])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True, risk=ToolRiskLevel.WRITE)},
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "WRITE_OUTCOME_AMBIGUOUS"

    def test_ambiguous_dangerous_write_gives_up(self) -> None:
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.DANGEROUS)])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True, risk=ToolRiskLevel.DANGEROUS)},
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "WRITE_OUTCOME_AMBIGUOUS"

    def test_upstream_5xx_on_write_gives_up(self) -> None:
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.WRITE)])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={
                "s1": _failed(
                    "s1",
                    is_retryable=True,
                    risk=ToolRiskLevel.WRITE,
                    error_code="UPSTREAM_503",
                )
            },
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "WRITE_OUTCOME_AMBIGUOUS"

    def test_ambiguous_write_gives_up_in_replanning_too(self) -> None:
        # An executor that marks an ambiguous write permanent (is_retryable=False,
        # e.g. the MCP write boundary) routes to REPLANNING; the pre-branch gate
        # still catches it so replan cannot re-invoke the write.
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.WRITE)])
        state = _state(
            status=AgentStatus.REPLANNING,
            plan=plan,
            step_results={
                "s1": _failed(
                    "s1",
                    is_retryable=False,
                    risk=ToolRiskLevel.WRITE,
                    error_code="TIMEOUT",
                )
            },
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "WRITE_OUTCOME_AMBIGUOUS"

    def test_read_ambiguous_failure_still_retries(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True)},
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.EXECUTING
        assert updates["retry_count"] == 1

    def test_permanent_write_failure_still_replans(self) -> None:
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.WRITE)])
        state = _state(
            status=AgentStatus.REPLANNING,
            plan=plan,
            step_results={
                "s1": _failed(
                    "s1",
                    is_retryable=False,
                    risk=ToolRiskLevel.WRITE,
                    error_code="PERMISSION_DENIED",
                )
            },
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.PLANNING


class TestReplanBranch:
    def test_permanent_failure_replans(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _state(
            status=AgentStatus.REPLANNING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=False)},
            errors=[StateError(code="PERMISSION_DENIED", message="denied", step_id="s1")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.PLANNING
        assert updates["replan_count"] == 1
        assert updates["plan"] is None
        assert updates["plan_validation"] is None
        assert updates["policy_decisions"] == {}
        assert updates["errors"] == []

    def test_replan_preserves_completed_step_results(self) -> None:
        # A completed WRITE step's result must survive replan so the resume
        # guard skips it on the new pass — at-most-once (deterministic planner
        # reuses positional step ids like s1/s2). The node omits step_results
        # from its updates; the graph merge keeps the existing completed ones.
        plan = Plan(steps=[_step("s1", risk_level=ToolRiskLevel.WRITE)])
        state = _state(
            status=AgentStatus.REPLANNING,
            plan=plan,
            step_results={"s1": StepResult(step_id="s1", status=StepStatus.COMPLETED)},
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.PLANNING
        assert "step_results" not in updates

    def test_validate_error_routes_to_replan(self) -> None:
        state = _state(
            status=AgentStatus.PLANNING,
            plan=None,
            errors=[StateError(code="UNKNOWN_TOOL", message="ghost", step_id="s1")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.PLANNING
        assert updates["replan_count"] == 1

    def test_replan_budget_exhausted_gives_up(self) -> None:
        state = _state(status=AgentStatus.REPLANNING, replan_count=MAX_REPLANS)
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "REPLAN_BUDGET_EXHAUSTED"


class TestHumanIntervention:
    def test_reconciliation_error_short_circuits_to_failed(self) -> None:
        state = _state(
            status=AgentStatus.RETRYING,
            errors=[StateError(code=RECOVERY_RECONCILIATION_REQUIRED, message="reconcile")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "RECOVERY_REQUIRES_HUMAN"

    def test_reconciliation_error_wins_over_replan_budget(self) -> None:
        state = _state(
            status=AgentStatus.REPLANNING,
            errors=[StateError(code=RECOVERY_RECONCILIATION_REQUIRED, message="reconcile")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "RECOVERY_REQUIRES_HUMAN"


class TestNoAction:
    def test_succeeded_without_errors_is_a_noop(self) -> None:
        updates = _invoke(_state(status=AgentStatus.SUCCEEDED))
        assert updates == {}


class TestRecoveryMetrics:
    """Task 7.3: each decision family advances its own Prometheus counter.

    The counters are the D2 retry_rate/recovery_rate source — the node incs
    them where the decision is made, keyed by semantic (retries/replans/
    abandoned) so aggregations can tell "retried a lot" from "gave up a lot".
    """

    def _wired(self, monkeypatch: pytest.MonkeyPatch) -> object:
        metrics = create_metrics()
        monkeypatch.setattr(recover_mod, "METRICS", metrics)
        return metrics

    def test_retry_branch_increments_retries_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = self._wired(monkeypatch)
        plan = Plan(steps=[_step("s1")])
        state = _state(
            status=AgentStatus.RETRYING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=True)},
        )
        assert _invoke(state)["status"] == AgentStatus.EXECUTING

        text = generate_latest(metrics)
        assert "erp_run_retries_total 1.0" in text
        assert "erp_run_replans_total 0.0" in text
        assert "erp_run_abandoned_total 0.0" in text

    def test_replan_branch_increments_replans_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = self._wired(monkeypatch)
        plan = Plan(steps=[_step("s1")])
        state = _state(
            status=AgentStatus.REPLANNING,
            plan=plan,
            step_results={"s1": _failed("s1", is_retryable=False)},
        )
        assert _invoke(state)["status"] == AgentStatus.PLANNING

        text = generate_latest(metrics)
        assert "erp_run_replans_total 1.0" in text
        assert "erp_run_retries_total 0.0" in text

    def test_give_up_branch_increments_abandoned_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = self._wired(monkeypatch)
        # No plan in the RETRYING branch routes to RECOVERY_GIVE_UP.
        state = _state(status=AgentStatus.RETRYING)
        assert _invoke(state)["status"] == AgentStatus.FAILED

        text = generate_latest(metrics)
        assert "erp_run_abandoned_total 1.0" in text
        assert "erp_run_retries_total 0.0" in text

    def test_noop_does_not_increment_any_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = self._wired(monkeypatch)
        assert _invoke(_state(status=AgentStatus.SUCCEEDED)) == {}

        text = generate_latest(metrics)
        assert "erp_run_retries_total 0.0" in text
        assert "erp_run_replans_total 0.0" in text
        assert "erp_run_abandoned_total 0.0" in text
