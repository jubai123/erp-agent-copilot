"""Deterministic recovery-sink node — tasks 4.11/5.8.

The graph's single recovery sink: whenever validate_plan or verify_results
reports errors, the routing edges land here to decide retry (back to
execute_ready_steps) vs replan (back to build_plan) vs give up (finalize ->
FAILED). The decision is bounded by run-level budgets so the loop always
terminates — every continuing decision strictly increments retry_count or
replan_count, both capped.

At-most-once is enforced here, not in the executor: a WRITE/DANGEROUS step that
failed without an idempotency key has an uncertain outcome (a timeout may have
applied the write), so it is never auto-re-run. Completed step results survive
both a retry and a replan — the node leaves step_results untouched and the
executor's resume guard skips COMPLETED steps on the next pass, which matters
because the deterministic planner reuses positional step ids (s1/s2) across
regenerations. A run annotated with RECOVERY_RECONCILIATION_REQUIRED (crash
recovery, RunRecovery) gives up for a human instead of auto-resolving.

Known limitation: on a replan, a COMPLETED step keeps its old result even if the
new plan intends it differently (resume guard wins). For the deterministic
worker this is a non-issue (same query -> same plan); an LLM planner that wants
to re-run a completed READ step would need per-step read/write handling here.
"""

from __future__ import annotations

from typing import Any

from erp_copilot.agent.recovery import RECOVERY_RECONCILIATION_REQUIRED
from erp_copilot.agent.state import AgentState, AgentStatus, StateError
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.observability.metrics import METRICS

# Run-level recovery budgets. Both counters live on AgentState; a mixed
# retry/replan sequence terminates within (MAX_RETRIES + 1) * (MAX_REPLANS + 1)
# recovery passes, well under the graph's recursion limit.
MAX_RETRIES = 2
MAX_REPLANS = 1


def _give_up(state: AgentState, code: str, message: str) -> dict[str, Any]:
    # Task 7.3: every give-up path funnels through here, so a single inc covers
    # human reconciliation, no-plan, unsafe write retry, and budget exhaustion.
    METRICS.runs_abandoned.inc()
    return {
        "status": AgentStatus.FAILED,
        "errors": [*state.errors, StateError(code=code, message=message)],
    }


def _failed_write_lacks_idempotency(state: AgentState) -> bool:
    """True when a failed WRITE/DANGEROUS step has no idempotency key.

    A transient failure (e.g. timeout) on such a step leaves its write outcome
    unknown; auto-retrying would break at-most-once, so the run gives up.
    """
    if state.plan is None:
        return False
    for step in state.plan.steps:
        if step.risk_level not in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
            continue
        result = state.step_results.get(step.step_id)
        if result is None or result.status != StepStatus.FAILED:
            continue
        if step.idempotency_key is None:
            return True
    return False


def recover_or_replan(state: AgentState) -> dict[str, Any]:
    """Decide retry vs replan vs give-up for a run routed to the recovery sink.

    Decision order:
    1. An error requiring human reconciliation gives up immediately — an
       uncertain write outcome is never auto-resolved.
    2. RETRYING retries the failed steps unless a failed write lacks an
       idempotency key, there is no plan, or the retry budget is exhausted.
    3. REPLANNING (permanent failure from verify) / PLANNING (invalid plan from
       validate) regenerates the plan unless the replan budget is exhausted.
    4. Anything else (e.g. SUCCEEDED with leftover non-step errors) is a no-op.
    """
    if any(e.code == RECOVERY_RECONCILIATION_REQUIRED for e in state.errors):
        return _give_up(
            state,
            "RECOVERY_REQUIRES_HUMAN",
            "存在结果不确定的写操作，需人工对账确认后再继续",
        )

    if state.status == AgentStatus.RETRYING:
        if state.plan is None:
            return _give_up(state, "RECOVERY_GIVE_UP", "无可重试的 plan")
        if _failed_write_lacks_idempotency(state):
            return _give_up(
                state,
                "WRITE_RETRY_UNSAFE",
                "写步骤无幂等键且结果不确定，禁止自动重试",
            )
        if state.retry_count >= MAX_RETRIES:
            return _give_up(
                state,
                "RETRY_BUDGET_EXHAUSTED",
                f"重试预算（{MAX_RETRIES}）已耗尽，放弃自动重试",
            )
        METRICS.runs_retries.inc()
        return {
            "status": AgentStatus.EXECUTING,
            "retry_count": state.retry_count + 1,
            # The transient errors that triggered this detour are consumed;
            # keeping them would re-route here on the next pass (infinite loop).
            "errors": [],
        }

    if state.status in (AgentStatus.REPLANNING, AgentStatus.PLANNING):
        if state.replan_count >= MAX_REPLANS:
            return _give_up(
                state,
                "REPLAN_BUDGET_EXHAUSTED",
                f"重规划预算（{MAX_REPLANS}）已耗尽，放弃自动重规划",
            )
        # step_results is deliberately NOT reset: the resume guard uses it to
        # skip completed (possibly WRITE) steps on the replanned pass.
        METRICS.runs_replans.inc()
        return {
            "status": AgentStatus.PLANNING,
            "replan_count": state.replan_count + 1,
            "plan": None,
            "plan_validation": None,
            "policy_decisions": {},
            "errors": [],
        }

    return {}
