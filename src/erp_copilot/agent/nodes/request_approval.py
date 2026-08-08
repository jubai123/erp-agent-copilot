"""Human-approval gate node — task 5.2.

Sits between policy_check and execute_ready_steps. When policy decides any
step REQUIRE_APPROVAL, this node records a PENDING ApprovalRequest per step
and pauses the run in WAITING_APPROVAL. The graph ends here; a later re-invoke
(after a human approves via the task-5.2 decision API) finds the APPROVED
records, creates nothing new, and the graph routes on to execution.

The node is a pure function of AgentState: no injected dependencies, returns
state updates. It is idempotent over re-invoke — existing records are never
duplicated — which is what lets checkpoint-resume (task 4.12) reuse the same
state without double-requesting approvals.
"""

from __future__ import annotations

from typing import Any

from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    ApprovalStatus,
    PolicyDecision,
)


def pending_approval_step_ids(state: AgentState) -> list[str]:
    """Step ids still awaiting a decision (no record, or record still PENDING).

    Public because the graph routes on the same signal — a step that needs a
    human decision must keep the run paused regardless of how it entered the
    plan.
    """
    decisions = state.policy_decisions
    records = {r.step_id: r for r in state.approvals}
    pending: list[str] = []
    if state.plan is not None:
        for step in state.plan.steps:
            if decisions.get(step.step_id) is not PolicyDecision.REQUIRE_APPROVAL:
                continue
            record = records.get(step.step_id)
            if record is None or record.status == ApprovalStatus.PENDING:
                pending.append(step.step_id)
    return pending


def _resolve_decided_approvals(state: AgentState) -> dict[str, PolicyDecision]:
    """Map decided approval records back onto policy so the resumed run routes.

    On resume the policy node re-decides REQUIRE_APPROVAL for the same WRITE
    step; execute_ready_steps only runs ALLOW steps. APPROVED therefore
    resolves to ALLOW (so the step executes) and DENIED to DENY (so the step
    is skipped). PENDING records stay untouched and keep the run waiting.
    """
    records = {r.step_id: r for r in state.approvals}
    resolved: dict[str, PolicyDecision] = {}
    if state.plan is None:
        return resolved
    for step in state.plan.steps:
        if state.policy_decisions.get(step.step_id) is not PolicyDecision.REQUIRE_APPROVAL:
            continue
        record = records.get(step.step_id)
        if record is None:
            continue
        if record.status == ApprovalStatus.APPROVED:
            resolved[step.step_id] = PolicyDecision.ALLOW
        elif record.status == ApprovalStatus.DENIED:
            resolved[step.step_id] = PolicyDecision.DENY
    return resolved


def request_approval_node(state: AgentState) -> dict[str, Any]:
    """Pause the run if any plan step is awaiting human approval."""
    if state.plan is None:
        return {}

    records = {r.step_id: r for r in state.approvals}
    new_requests: list[ApprovalRequest] = []
    for step in state.plan.steps:
        if state.policy_decisions.get(step.step_id) is not PolicyDecision.REQUIRE_APPROVAL:
            continue
        if step.step_id in records:
            continue  # already recorded — idempotent across re-invoke
        new_requests.append(
            ApprovalRequest(
                step_id=step.step_id,
                tool_name=step.tool_name,
                description=step.description,
                risk_level=step.risk_level,
                required_scope=step.required_scope,
            )
        )

    updates: dict[str, Any] = {}
    if new_requests:
        updates["approvals"] = [*state.approvals, *new_requests]
    resolved = _resolve_decided_approvals(state)
    if resolved:
        updates["policy_decisions"] = {**state.policy_decisions, **resolved}
    if pending_approval_step_ids(state):
        updates["status"] = AgentStatus.WAITING_APPROVAL
    return updates
