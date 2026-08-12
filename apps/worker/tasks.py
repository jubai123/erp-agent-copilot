"""Celery tasks for asynchronous run execution.

Task 4.16: the worker drives the full LangGraph instead of a direct simulator
shortcut. execute_run seeds an AgentState from the Run row, runs
build_agent_graph with real nodes (deterministic planner, validate, policy,
ERP-simulator executor, verify) and a CheckpointSaver bound to the DB session,
then maps the terminal runtime state back onto the Run row and RunStep rows.
Task 5.8/5.9: a re-entered run resumes from its latest checkpoint via
RunRecovery instead of restarting blank, and a FAILED/EXPIRED terminal routes
through FailureQueue so the run lands in the human-intervention queue.
Lifecycle: QUEUED -> PROCESSING -> COMPLETED | FAILED | ...
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from celery import Task
from celery.utils.log import get_task_logger
from langgraph.graph.state import CompiledStateGraph

from apps.worker.celery_app import celery_app
from apps.worker.graph_builder import build_worker_graph
from erp_copilot.agent.recovery import RunRecovery
from erp_copilot.agent.state import AgentState, AgentStatus, StateError
from erp_copilot.application.events import append_run_event
from erp_copilot.application.failure_queue import FailureQueue
from erp_copilot.domain.entities import Run, RunStep
from erp_copilot.domain.enums import StepStatus
from erp_copilot.memory.checkpoint import CheckpointSaver

logger = get_task_logger(__name__)

# The worst retry/replan sequence (2 retries -> 1 replan -> give up) visits
# ~24 nodes, close to LangGraph's default recursion_limit of 25; raise it so a
# budgeted recovery loop never trips GraphRecursionError.
_RECURSION_LIMIT = 50

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


def _default_query(product_name: str) -> str:
    """Compose the natural-language query the deterministic planner reads.

    The API only passes a product name; the query is what classify_intent and
    the planner operate on, so it is built to include the product name. Callers
    may override it (e.g. the two-step "查苹果库存并推荐供应商" demo) via the
    query task argument.
    """
    return f"查询{product_name}库存"


def _make_status_event_sink(session) -> Callable[[str, AgentState], None]:
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


def _invoke_graph(graph: CompiledStateGraph, state: AgentState) -> AgentState:
    """Run the compiled graph and return the terminal AgentState.

    Celery eager mode inside FastAPI's event loop means a running loop may
    already exist (asyncio.run would raise); the graph then runs in a dedicated
    worker thread with its own loop. A normal prefork worker has no running
    loop and runs inline.
    """
    state_dict = state.model_dump()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result: dict = asyncio.run(
            graph.ainvoke(state_dict, config={"recursion_limit": _RECURSION_LIMIT})
        )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                lambda: asyncio.run(
                    graph.ainvoke(state_dict, config={"recursion_limit": _RECURSION_LIMIT})
                )
            ).result()

    return AgentState.model_validate(result)


def _persist(session, run: Run, final: AgentState) -> str:
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
    return run.status


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def execute_run(
    self: Task,
    run_id: str,
    product_name: str = "苹果",
    query: str | None = None,
    deadline_s: int | None = None,
) -> dict:
    """Drive one run through the LangGraph and persist the outcome.

    Lifecycle: QUEUED -> PROCESSING -> COMPLETED | FAILED | ...
    A positive *deadline_s* seeds AgentState.deadline_at so the check_deadline
    node (task 4.15) expires the run as EXPIRED -> FAILED if it is still going
    at the deadline; None leaves the run without a deadline.
    """
    from erp_copilot.infrastructure.database import get_session

    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            logger.error("Run %s not found", run_id)
            return {"status": "error", "detail": f"Run {run_id} not found"}

        # A run cancelled (or otherwise settled) before the worker picked it up
        # must not be re-processed — cancel sticks, never clobbered.
        if run.status in {"CANCELLED", "COMPLETED", "FAILED"}:
            logger.info("Run %s already %s, skipping", run_id, run.status)
            return {"status": run.status, "run_id": run.id}

        run.status = "PROCESSING"
        run.started_at = datetime.now(UTC)
        session.commit()

        saver = CheckpointSaver(session, event_sink=_make_status_event_sink(session))
        graph = build_worker_graph(session, saver)

        # Task 5.8: a run that was mid-flight when the worker (re)started resumes
        # from its latest checkpoint (reconciled for writes) instead of restarting
        # from a blank state — otherwise a decided approval or partial execution
        # would be lost. A fresh run has no checkpoint and starts blank.
        recovery = RunRecovery(session, saver=saver)
        resumed = recovery.load(run.id, run.tenant_id)
        if resumed.resumed and resumed.state is not None:
            state = resumed.state
        else:
            state = AgentState(
                run_id=run.id,
                tenant_id=run.tenant_id,
                # No per-user identity is carried by create_run yet; the run
                # executes as a system actor and the policy gate resolves scopes.
                user_id=run.user_id or "system",
                query=query or _default_query(product_name),
                deadline_at=(
                    datetime.now(UTC) + timedelta(seconds=deadline_s)
                    if deadline_s is not None
                    else None
                ),
            )
        final = _invoke_graph(graph, state)
        status = _persist(session, run, final)
        logger.info("Run %s finished with status %s", run_id, status)
        return {"status": status, "run_id": run.id}
    except Exception:
        session.rollback()
        try:
            run = session.query(Run).filter_by(id=run_id).first()
            if run:
                run.status = "FAILED"
                run.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()
