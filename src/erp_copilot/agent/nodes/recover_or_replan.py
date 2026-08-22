"""Deterministic recovery-sink node — tasks 4.11/5.8.

The graph's single recovery sink: whenever validate_plan or verify_results
reports errors, the routing edges land here to decide retry (back to
execute_ready_steps) vs replan (back to build_plan) vs give up (finalize ->
FAILED). The decision is bounded by run-level budgets so the loop always
terminates — every continuing decision strictly increments retry_count or
replan_count, both capped.

At-most-once is enforced here, not in the executor: a WRITE/DANGEROUS step that
failed with an ambiguous transient code (TIMEOUT/UPSTREAM_UNAVAILABLE/UPSTREAM_5xx)
has an uncertain outcome — a timeout may have applied the write, and the cloud
ERP has no server-side idempotency to absorb a re-run — so it is never
auto-re-run by a retry or a replan; the run gives up for a human to reconcile
via a read-back. Completed step results survive both a retry and a replan — the
node leaves step_results untouched and the executor's resume guard skips
COMPLETED steps on the next pass, which matters because the deterministic
planner reuses positional step ids (s1/s2) across regenerations. A run annotated
with RECOVERY_RECONCILIATION_REQUIRED (crash recovery, RunRecovery) gives up for
a human instead of auto-resolving.

Known limitation: on a replan, a COMPLETED step keeps its old result even if the
new plan intends it differently (resume guard wins). For the deterministic
worker this is a non-issue (same query -> same plan); an LLM planner that wants
to re-run a completed READ step would need per-step read/write handling here.
"""

from __future__ import annotations

from typing import Any

from erp_copilot.agent.compensation import (
    PARTIAL_WRITE_DETECTED,
    detect_partial_writes,
    is_ambiguous_code,
)
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
    # human reconciliation, no-plan, ambiguous write outcome, and budget exhaustion.
    METRICS.runs_abandoned.inc()
    return {
        "status": AgentStatus.FAILED,
        "errors": [*state.errors, StateError(code=code, message=message)],
    }


def _maybe_compensate_give_up(state: AgentState, code: str, message: str) -> dict[str, Any]:
    """Give up, and when a partial write exists flag it for automatic compensation.

    Budget exhaustion is the only give-up family that compensates: the run can
    no longer retry/replan, so the completed earlier writes would otherwise
    dangle. Ambiguous-write and human-reconciliation give-ups return plain
    _give_up — their outcomes are uncertain, so a compensating write is unsafe.
    The PARTIAL_WRITE_DETECTED error is appended after the give-up code, so
    persist_run's failure_code (errors[0]) is unchanged.
    """
    updates = _give_up(state, code, message)
    if detect_partial_writes(state.plan, state.step_results):
        updates["compensation_pending"] = True
        updates["errors"] = [
            *updates["errors"],
            StateError(
                code=PARTIAL_WRITE_DETECTED,
                message="检测到已完成的部分写步骤，自动补偿已启动",
            ),
        ]
    return updates


def _failed_ambiguous_write(state: AgentState) -> bool:
    """True when a failed WRITE/DANGEROUS step carries an ambiguous transient code.

    The ambiguous codes mirror the executors' retryable taxonomy (TIMEOUT /
    UPSTREAM_UNAVAILABLE / UPSTREAM_5xx): the request may have been applied
    server-side before the failure surfaced, so the write outcome is unknown.
    The cloud ERP has no server-side idempotency (createOrder drops the caller's
    key), so neither a retry nor a replan may re-invoke it — the run gives up for
    a human to reconcile via a read-back.
    """
    if state.plan is None:
        return False
    for step in state.plan.steps:
        if step.risk_level not in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
            continue
        result = state.step_results.get(step.step_id)
        if result is None or result.status != StepStatus.FAILED:
            continue
        if is_ambiguous_code(result.error_code):
            return True
    return False


def recover_or_replan(state: AgentState) -> dict[str, Any]:
    """Decide retry vs replan vs give-up for a run routed to the recovery sink.

    Decision order:
    1. An error requiring human reconciliation gives up immediately — an
       uncertain write outcome is never auto-resolved.
    2. A failed WRITE/DANGEROUS step carrying an ambiguous transient code gives
       up for a human: the cloud may have applied the write, so neither retry
       nor replan may re-invoke it. This sits before the status branches so the
       replan path is covered too.
    3. RETRYING retries the failed steps unless there is no plan or the retry
       budget is exhausted.
    4. REPLANNING (permanent failure from verify) / PLANNING (invalid plan from
       validate) regenerates the plan unless the replan budget is exhausted.
    5. Anything else (e.g. SUCCEEDED with leftover non-step errors) is a no-op.
    """
    if any(e.code == RECOVERY_RECONCILIATION_REQUIRED for e in state.errors):
        return _give_up(
            state,
            "RECOVERY_REQUIRES_HUMAN",
            "存在结果不确定的写操作，需人工对账确认后再继续",
        )

    if _failed_ambiguous_write(state):
        return _give_up(
            state,
            "WRITE_OUTCOME_AMBIGUOUS",
            "写步骤以歧义性瞬态失败（TIMEOUT/UPSTREAM_5xx/UPSTREAM_UNAVAILABLE），"
            "云端可能已生效且无服务器端幂等，禁止自动重试/重规划，需人工对账",
        )

    if state.status == AgentStatus.RETRYING:
        if state.plan is None:
            return _give_up(state, "RECOVERY_GIVE_UP", "无可重试的 plan")
        if state.retry_count >= MAX_RETRIES:
            return _maybe_compensate_give_up(
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
            return _maybe_compensate_give_up(
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
