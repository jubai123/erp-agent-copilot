"""Saga compensation for partial multi-write DAG failures — 缺口二.

When a multi-write Plan DAG partially succeeds then fails permanently at
give-up time (recover_or_replan budget exhausted), the completed earlier
writes leave the business in a dangling state unless undone. The canonical
case is the modify DAG (s1 lookup → s2 cancelOrder → s3 createOrder): if s3
fails forever, s2 already cancelled the original order with no replacement.

This module is the deterministic compensation engine plus a dedicated graph
node that runs the compensating actions automatically — before finalize,
keeping the run FAILED. It supplements, never replaces, the human
reconciliation path.

Safety (the two gates that bound it):
- non-ambiguous only — a WRITE step that failed with TIMEOUT /
  UPSTREAM_UNAVAILABLE / UPSTREAM_5xx has an unknown outcome; compensating an
  unknown-outcome write risks a double effect, so those runs give up for a
  human instead (recover_or_replan's WRITE_OUTCOME_AMBIGUOUS gate);
- give-up only — compensation fires when recover_or_replan decides to abandon
  the run (RETRY_BUDGET_EXHAUSTED / REPLAN_BUDGET_EXHAUSTED), never while a
  retry or replan could still succeed and depend on the completed write.

Compensating writes are themselves writes, so they run through the same
at-most-once idempotency path as the original plan steps, keyed
f"{run_id}:comp:{step_id}".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from erp_copilot.agent.nodes.execute_steps import _run_one, _to_step_result
from erp_copilot.agent.retry_policy import AsyncRetryExecutor
from erp_copilot.agent.state import AgentState, Plan, PlanStep, StateError, StepResult
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.tools.idempotency import IdempotencyStore

PARTIAL_WRITE_DETECTED = "PARTIAL_WRITE_DETECTED"
WRITE_PARTIAL_COMPENSATED = "WRITE_PARTIAL_COMPENSATED"
WRITE_PARTIAL_UNCOMPENSATED = "WRITE_PARTIAL_UNCOMPENSATED"

# Transient failure codes whose write outcome is unknown — the request may
# have been applied server-side before the failure surfaced. Mirrors the
# HTTP/MCP executors' retryable-code taxonomy (apps/worker/executor.py,
# mcp_executor.py). Lives here (not in recover_or_replan) because both the
# recover gate and detect_partial_writes need it, and recover_or_replan
# imports from this module.
_AMBIGUOUS_TRANSIENT_CODES = frozenset({"TIMEOUT", "UPSTREAM_UNAVAILABLE"})
_UPSTREAM_STATUS_RE = re.compile(r"^UPSTREAM_(\d{3})$")


def is_ambiguous_code(code: str | None) -> bool:
    """True for a transient failure code whose write outcome is uncertain."""
    if not code:
        return False
    if code in _AMBIGUOUS_TRANSIENT_CODES:
        return True
    match = _UPSTREAM_STATUS_RE.match(code)
    return match is not None and 500 <= int(match.group(1)) < 600


@dataclass(frozen=True)
class CompensationSpec:
    """How to undo one completed write tool.

    argument_map maps each compensating-tool argument to the field of the
    completed step's own result data it should read — the compensation is
    self-contained, no cross-step references needed.
    """

    compensate_with: str
    argument_map: dict[str, str]


# Deterministic compensation registry. A tool is compensatable only when the
# opposite write can be reconstructed from the completed write's own result —
# createOrder returns its order_id (→ cancel it), cancelOrder returns the
# full order (→ re-create it), addProduct/addSuppliers return the new
# id/supplier_id (→ remove/delete it). update*/remove*/delete* need the
# pre-write value the runtime does not have, so they are deliberately absent.
_COMPENSATION_SPECS: dict[str, CompensationSpec] = {
    "createOrder": CompensationSpec(
        compensate_with="cancelOrder", argument_map={"order_id": "order_id"}
    ),
    "cancelOrder": CompensationSpec(
        compensate_with="createOrder",
        argument_map={
            "product_id": "product_id",
            "quantity": "quantity",
            "supplier_id": "supplier_id",
            "region": "region",
        },
    ),
    "addProduct": CompensationSpec(
        compensate_with="removeProductById", argument_map={"product_id": "product_id"}
    ),
    "addSuppliers": CompensationSpec(
        compensate_with="deleteSupplierById", argument_map={"supplier_id": "supplier_id"}
    ),
}


def _topological_order(plan: Plan) -> list[str]:
    """Kahn's toposort over plan.steps; returns [] on a cycle (nothing to do)."""
    indeg: dict[str, int] = {step.step_id: 0 for step in plan.steps}
    children: dict[str, list[str]] = {step.step_id: [] for step in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            if dep in indeg:
                children[dep].append(step.step_id)
                indeg[step.step_id] += 1
    order: list[str] = []
    remaining = set(indeg)
    while remaining:
        ready = sorted(sid for sid in remaining if indeg[sid] == 0)
        if not ready:
            return []
        for sid in ready:
            order.append(sid)
            remaining.discard(sid)
            for child in children[sid]:
                indeg[child] -= 1
    return order


def detect_partial_writes(
    plan: Plan | None,
    step_results: dict[str, StepResult],
) -> list[str]:
    """Completed WRITE/DANGEROUS steps to compensate, in reverse topo order.

    Fires only when a WRITE/DANGEROUS step failed with a NON-ambiguous code —
    the ambiguous codes are recover_or_replan's human gate and never reach
    here. The returned steps are the completed writes strictly before the
    first such failure, ordered reverse-topologically so the most recently
    executed write is compensated first.
    """
    if plan is None:
        return []
    order = _topological_order(plan)
    if not order:
        return []
    by_id = {step.step_id: step for step in plan.steps}

    first_failed: int | None = None
    for idx, step_id in enumerate(order):
        step = by_id[step_id]
        if step.risk_level not in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
            continue
        result = step_results.get(step_id)
        if result is None or result.status != StepStatus.FAILED:
            continue
        if is_ambiguous_code(result.error_code):
            continue
        first_failed = idx
        break
    if first_failed is None:
        return []

    completed: list[str] = []
    for step_id in order[:first_failed]:
        step = by_id[step_id]
        if step.risk_level not in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
            continue
        result = step_results.get(step_id)
        if result is not None and result.status == StepStatus.COMPLETED:
            completed.append(step_id)
    return list(reversed(completed))


@dataclass(frozen=True)
class CompensationAction:
    """One concrete compensating write, ready for the executor."""

    step_id: str
    tool_name: str
    arguments: dict[str, Any]


def build_compensation_steps(
    plan: Plan,
    step_results: dict[str, StepResult],
) -> list[CompensationAction]:
    """Map each detected completed write to its compensating action.

    A completed write is compensatable only when it has a registered spec and
    its own result carries every source field the compensating tool needs.
    Writes without a spec (update*/remove*/delete* — their rollback needs the
    pre-write value) are left out; the node reports them back as
    WRITE_PARTIAL_UNCOMPENSATED for a human.
    """
    by_id = {step.step_id: step for step in plan.steps}
    actions: list[CompensationAction] = []
    for step_id in detect_partial_writes(plan, step_results):
        spec = _COMPENSATION_SPECS.get(by_id[step_id].tool_name)
        if spec is None:
            continue
        result = step_results.get(step_id)
        data = result.data if result is not None else None
        if not isinstance(data, dict):
            continue
        arguments: dict[str, Any] = {}
        for arg_name, source_field in spec.argument_map.items():
            if source_field not in data:
                break
            arguments[arg_name] = data[source_field]
        else:
            actions.append(
                CompensationAction(
                    step_id=step_id,
                    tool_name=spec.compensate_with,
                    arguments=arguments,
                )
            )
    return actions


def _compensation_step(action: CompensationAction, run_id: str) -> PlanStep:
    """Wrap one compensation action as an idempotent WRITE plan step.

    The key is f"{run_id}:comp:{step_id}" — the run-level write key convention
    (stamp_idempotency_keys) extended with a comp marker, so a crash-resume
    replays instead of double-compensating.
    """
    key = f"{run_id}:comp:{action.step_id}"
    return PlanStep(
        step_id=f"comp-{action.step_id}",
        tool_name=action.tool_name,
        description=f"补偿已完成写步骤 {action.step_id}（{action.tool_name}）",
        arguments={**action.arguments, "idempotency_key": key},
        idempotency_key=key,
        risk_level=ToolRiskLevel.WRITE,
    )


def build_compensate_partial_writes_node(
    *,
    executor: Any,
    idempotency_store: IdempotencyStore | None = None,
    retry_executor: AsyncRetryExecutor | None = None,
) -> Any:
    """Build the compensate_partial_writes LangGraph node.

    Runs only when AgentState.compensation_pending is set (recover_or_replan's
    give-up branches). Each compensation action executes through the same
    at-most-once path as plan writes and is reported as a structured StateError
    — WRITE_PARTIAL_COMPENSATED on success, WRITE_PARTIAL_UNCOMPENSATED on
    compensation failure or a non-compensatable write. The run stays FAILED;
    the node only clears the pending flag.
    """

    async def compensate_partial_writes_node(state: AgentState) -> dict[str, Any]:
        if not state.compensation_pending:
            return {}
        if state.plan is None:
            return {"compensation_pending": False}

        detected = detect_partial_writes(state.plan, state.step_results)
        actions = build_compensation_steps(state.plan, state.step_results)
        actionable = {action.step_id for action in actions}
        uncompensatable = [step_id for step_id in detected if step_id not in actionable]

        results = dict(state.step_results)
        errors: list[StateError] = []
        for action in actions:
            step = _compensation_step(action, state.run_id)
            _, outcome = await _run_one(
                step,
                step.arguments,
                executor,
                idempotency_store,
                state.tenant_id,
                state.run_id,
                retry_executor,
            )
            results[step.step_id] = _to_step_result(step, outcome)
            if outcome.status == "SUCCEEDED":
                errors.append(
                    StateError(
                        code=WRITE_PARTIAL_COMPENSATED,
                        message=f"已完成写步骤 {action.step_id} 已自动补偿（{action.tool_name}）",
                        step_id=action.step_id,
                        details={"compensated_with": action.tool_name},
                    )
                )
            else:
                error = outcome.error
                errors.append(
                    StateError(
                        code=WRITE_PARTIAL_UNCOMPENSATED,
                        message=f"补偿步骤 {action.step_id} 失败（{action.tool_name}）",
                        step_id=action.step_id,
                        details={
                            "compensated_with": action.tool_name,
                            "error_code": error.error_code if error else "EXECUTION_FAILED",
                        },
                    )
                )
        for step_id in uncompensatable:
            errors.append(
                StateError(
                    code=WRITE_PARTIAL_UNCOMPENSATED,
                    message=f"已完成写步骤 {step_id} 无自动补偿动作，需人工处理",
                    step_id=step_id,
                )
            )

        updates: dict[str, Any] = {
            "compensation_pending": False,
            "step_results": results,
        }
        if errors:
            updates["errors"] = [*state.errors, *errors]
        return updates

    return compensate_partial_writes_node
