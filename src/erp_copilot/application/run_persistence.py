"""Terminal-state persistence for a finished graph pass — task 5.2 wiring.

Maps a terminal AgentState onto the Run row and RunStep rows, routing a
FAILED/EXPIRED outcome through the human-intervention queue (task 5.9). Shared
by the worker (execute_run) and the API approve/resume path so the status
mappings and failure handling stay single-source.

The module must stay Celery-free: apps.api imports it for the approve-run
resume path, and importing apps.worker.celery_app at API module load would
bootstrap the broker/client eagerly (same constraint as apps/worker/graph_builder).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from erp_copilot.agent.state import AgentState, AgentStatus, StateError
from erp_copilot.application.events import append_run_event
from erp_copilot.application.failure_queue import FailureQueue
from erp_copilot.domain.entities import Run, RunStep
from erp_copilot.domain.enums import StepStatus
from erp_copilot.observability.metrics import METRICS

# Runtime AgentStatus -> persisted Run.status. create_run and the pre-LangGraph
# worker use uppercase strings; the checkpoint layer separately stores the
# lowercase RunStatus view (map_agent_status) — the two lifecycles stay apart.
_RUN_STATUS_UPPER: dict[AgentStatus, str] = {
    AgentStatus.SUCCEEDED: "COMPLETED",
    AgentStatus.WAITING_APPROVAL: "WAITING_APPROVAL",
    AgentStatus.CANCELLED: "CANCELLED",
    AgentStatus.FAILED: "FAILED",
    AgentStatus.EXPIRED: "FAILED",
    # The recover_or_replan node resolves RETRYING/REPLANNING inside the graph
    # (to EXECUTING/PLANNING or FAILED), so these are safety nets — kept in case
    # a future path lets an in-flight state escape the graph.
    AgentStatus.QUEUED: "FAILED",
    AgentStatus.PLANNING: "FAILED",
    AgentStatus.EXECUTING: "FAILED",
    AgentStatus.VERIFYING: "FAILED",
    AgentStatus.RETRYING: "FAILED",
    AgentStatus.REPLANNING: "FAILED",
}

_STEP_STATUS_UPPER: dict[StepStatus, str] = {
    StepStatus.PENDING: "PENDING",
    StepStatus.IN_PROGRESS: "IN_PROGRESS",
    StepStatus.COMPLETED: "COMPLETED",
    StepStatus.FAILED: "FAILED",
    StepStatus.SKIPPED: "SKIPPED",
}

# docs/03 §9 error codes -> operator guidance for FailureQueue.suggested_action.
# Keys cover the failures the worker graph can actually emit; anything else
# falls back to a generic instruction.
_SUGGESTED_ACTION: dict[str, str] = {
    "RECOVERY_RECONCILIATION_REQUIRED": "核对 ERP 侧写操作是否已生效，再决定重试或人工处理",
    "RECOVERY_REQUIRES_HUMAN": "人工对账结果不确定的写操作后重试",
    "TIMEOUT": "检查 ERP Simulator 可用性后重试",
    "DEADLINE_EXCEEDED": "确认截止时间配置后重新发起",
    "EMPTY_PLAN": "补充查询条件（商品名/订单号/供应商意图）后重试",
    "RETRY_BUDGET_EXHAUSTED": "检查持续失败根因后人工重试",
    "REPLAN_BUDGET_EXHAUSTED": "检查计划生成失败根因后人工处理",
    "WRITE_RETRY_UNSAFE": "核对写操作是否已生效后人工处理",
    "RECOVERY_GIVE_UP": "检查恢复失败原因后人工处理",
    "INSUFFICIENT_STOCK": "确认可用库存后调整下单数量",
    "PRODUCT_NOT_FOUND": "确认商品名称/编号正确后重试",
}


def _suggested_action(code: str) -> str:
    return _SUGGESTED_ACTION.get(code, "检查失败原因并确认修复后重试")


def make_status_event_sink(session: Session) -> Callable[[str, AgentState], None]:
    """Record one RUN_STATUS event per runtime status change (task 4.14).

    The checkpoint saver persists after every node; this sink turns the
    status transitions among those saves into a deduped, replayable event
    log for the SSE stream. The runtime starts QUEUED, so the first emitted
    status is the first that differs (e.g. planning).
    """
    last_status: AgentStatus = AgentStatus.QUEUED

    def sink(node_name: str, state: AgentState) -> None:
        nonlocal last_status
        if state.status == last_status:
            return
        last_status = state.status
        append_run_event(
            session,
            state.run_id,
            "RUN_STATUS",
            {"status": state.status.value, "node": node_name},
        )

    return sink


def persist_run(session: Session, run: Run, final: AgentState) -> str:
    """Map the terminal AgentState onto the Run row and RunStep rows.

    The run row is re-read first so a cancel that landed while the graph ran
    is never clobbered: a settled CANCELLED run stays cancelled (the same
    settled-run rule FailureQueue enforces). A FAILED/EXPIRED terminal routes
    through FailureQueue.record, which sets the three diagnostic fields, bumps
    version and appends the RUN_FAILED event.
    """
    fresh = session.query(Run).filter_by(id=run.id).first()
    if fresh is None or fresh.status == "CANCELLED":
        return fresh.status if fresh is not None else run.status
    run = fresh
    if final.status in (AgentStatus.FAILED, AgentStatus.EXPIRED):
        # Task 5.9: a terminal failure routes through FailureQueue so it lands
        # in the human-intervention queue with a machine-readable code, a
        # human-readable reason, a suggested action, and an immutable
        # RUN_FAILED event. FailureQueue re-guards COMPLETED/CANCELLED so a
        # settled run is never clobbered even if this branch races one.
        first = final.errors[0] if final.errors else StateError(code="UNKNOWN", message="未知错误")
        FailureQueue(session).record(
            run_id=run.id,
            tenant_id=run.tenant_id,
            error_code=first.code,
            reason=first.message,
            suggested_action=_suggested_action(first.code),
        )
    else:
        run.status = _RUN_STATUS_UPPER[final.status]
        if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            run.completed_at = datetime.now(UTC)
    steps = {step.step_id: step for step in final.plan.steps} if final.plan else {}
    for index, (step_id, step) in enumerate(steps.items(), start=1):
        result = final.step_results.get(step_id)
        session.add(
            RunStep(
                run_id=run.id,
                step_index=index,
                step_type="TOOL_CALL",
                status=_STEP_STATUS_UPPER.get(result.status, "PENDING") if result else "SKIPPED",
                input=json.dumps(step.arguments, ensure_ascii=False),
                output=(
                    json.dumps(result.data, ensure_ascii=False) if result and result.data else None
                ),
                error_message=result.error_message if result else None,
                started_at=result.started_at if result else None,
                completed_at=result.finished_at if result else None,
            )
        )
    session.commit()
    # Only a terminal run counts: a run pausing at WAITING_APPROVAL is created
    # but not yet settled, and a run cancelled while the graph ran returned
    # early above, so neither reaches the counters here.
    if run.status == "COMPLETED":
        METRICS.runs_completed.inc()
    elif run.status == "FAILED":
        METRICS.runs_failed.inc()
    return run.status
