"""Approve/deny decision API and checkpoint replay — task 5.2.

The request_approval gate pauses a run in WAITING_APPROVAL with PENDING
ApprovalRequest records. ApprovalDecisionService flips one PENDING record to
APPROVED or DENIED and persists the decided state as the newest checkpoint;
resume_run then re-invokes the graph from that checkpoint so the run
continues — request_approval's policy resolution makes an APPROVED step
execute and a DENIED step skip.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from erp_copilot.agent.state import AgentStatus, ApprovalRequest, ApprovalStatus
from erp_copilot.domain.entities import AuditLog
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.observability.metrics import METRICS

_DECIDABLE = (ApprovalStatus.APPROVED, ApprovalStatus.DENIED)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalDecisionService:
    """Flip a PENDING approval request and persist the decided state.

    The saver is injected (the app passes the checkpoint saver bound to the
    run's database session); the clock is a seam so tests can order the
    decision timestamp deterministically.
    """

    def __init__(
        self,
        saver: CheckpointSaver,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._saver = saver
        self._clock = clock

    def decide(
        self,
        *,
        run_id: str,
        tenant_id: str,
        step_id: str,
        decision: ApprovalStatus,
        decided_by: str,
        reason: str | None = None,
        modified_plan: str | None = None,
    ) -> ApprovalRequest:
        """Resolve one PENDING request and save the decided state as a checkpoint.

        The saved checkpoint is what resume_run replays: re-invoking the graph
        from the decided state routes the step onward. Returns the decided
        record so callers (HTTP layer, audit) see who/when/why without a second
        read. *modified_plan* (task 7.8) is the user's edited plan when approving
        with changes — recorded on the decided record for audit and mapped to
        the plan-outcome bucket (approved -> accepted, approved+edits -> edited,
        denied -> rejected).
        """
        if decision not in _DECIDABLE:
            raise CopilotError(
                f"decision must be APPROVED or DENIED, got {decision!r}",
                code="APPROVAL_DECISION_INVALID",
            )

        state = self._saver.load_latest(run_id, tenant_id)
        if state is None:
            raise NotFoundError(
                f"no checkpoint for run {run_id!r}",
                code="CHECKPOINT_NOT_FOUND",
            )
        if state.status != AgentStatus.WAITING_APPROVAL:
            raise CopilotError(
                f"run {run_id!r} is not waiting for approval",
                code="RUN_NOT_WAITING_APPROVAL",
            )

        record = next((r for r in state.approvals if r.step_id == step_id), None)
        if record is None:
            raise NotFoundError(
                f"no approval request for step {step_id!r}",
                code="APPROVAL_NOT_FOUND",
            )
        if record.status != ApprovalStatus.PENDING:
            raise CopilotError(
                f"approval for step {step_id!r} is already decided",
                code="APPROVAL_ALREADY_DECIDED",
            )

        decided = record.model_copy(
            update={
                "status": decision,
                "decided_by": decided_by,
                "decided_at": self._clock(),
                "reason": reason,
                "modified_plan": modified_plan,
            }
        )
        approvals = [decided if r.step_id == step_id else r for r in state.approvals]
        self._saver.save(
            "approval_decision",
            state.model_copy(update={"approvals": approvals}),
        )
        # Tasks 7.4/7.8: inc after the checkpoint save — a failed persist leaves
        # no record and no count, so metrics stay consistent with the audit trail.
        METRICS.approval_requests.labels(outcome=decision.value).inc()
        outcome = "rejected" if decision == ApprovalStatus.DENIED else (
            "edited" if modified_plan else "accepted"
        )
        METRICS.plan_outcomes.labels(outcome=outcome).inc()
        return decided


async def resume_run(
    graph: CompiledStateGraph,
    saver: CheckpointSaver,
    *,
    run_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Re-invoke the graph from the latest checkpoint, continuing the run.

    Raises NotFoundError (CHECKPOINT_NOT_FOUND) if no checkpoint exists. The
    graph is a compiled LangGraph; the state dict feeds graph.ainvoke. Since
    the decided state was saved as the newest checkpoint, request_approval
    resolves decided steps and the run proceeds — or skips denied steps.
    """
    state = saver.load_latest(run_id, tenant_id)
    if state is None:
        raise NotFoundError(
            f"no checkpoint for run {run_id!r}",
            code="CHECKPOINT_NOT_FOUND",
        )
    result: dict[str, Any] = await graph.ainvoke(state.model_dump())
    return result


def record_audit_log(
    session: Session,
    *,
    actor: str,
    resource: str,
    action: str,
    result: str,
    ip: str | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    """Append one immutable audit_logs row (docs/07 §6).

    created_at is default-only (no onupdate) so the timestamp never moves.
    The row is committed immediately — a decision must be audited even when
    the resume that follows fails.
    """
    entry = AuditLog(
        actor=actor,
        resource=resource,
        action=action,
        result=result,
        ip=ip,
        trace_id=trace_id,
    )
    session.add(entry)
    session.commit()
    return entry


async def decide_and_resume(
    graph: CompiledStateGraph,
    saver: CheckpointSaver,
    session: Session,
    *,
    run_id: str,
    tenant_id: str,
    step_id: str,
    decision: ApprovalStatus,
    decided_by: str,
    reason: str | None = None,
    modified_plan: str | None = None,
    ip: str | None = None,
    trace_id: str | None = None,
) -> tuple[ApprovalRequest, dict[str, Any]]:
    """Decide, audit, then resume — the approve/deny API orchestration.

    Order matters: flip and checkpoint the decision first, write the audit row
    second, resume the graph last. *session* is the audit session; the route
    passes the same session the checkpoint saver uses.
    """
    decided = ApprovalDecisionService(saver).decide(
        run_id=run_id,
        tenant_id=tenant_id,
        step_id=step_id,
        decision=decision,
        decided_by=decided_by,
        reason=reason,
        modified_plan=modified_plan,
    )
    record_audit_log(
        session,
        actor=decided_by,
        resource=f"run:{run_id}/step:{step_id}",
        action=f"approval.{decision.value}",
        result=decision.value,
        ip=ip,
        trace_id=trace_id,
    )
    result = await resume_run(graph, saver, run_id=run_id, tenant_id=tenant_id)
    return decided, result
