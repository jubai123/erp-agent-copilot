"""Celery tasks for asynchronous run execution.

Task 4.16: the worker drives the full LangGraph instead of a direct simulator
shortcut. execute_run seeds an AgentState from the Run row, runs
build_agent_graph with real nodes (deterministic planner, validate, policy,
ERP-simulator executor, verify) and a CheckpointSaver bound to the DB session,
then maps the terminal runtime state back onto the Run row and RunStep rows.
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
from apps.worker.executor import erp_simulator_executor
from erp_copilot.agent.graph import build_agent_graph
from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node
from erp_copilot.agent.nodes.policy_check import build_policy_check_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.nodes.verify_results import build_verify_results_node
from erp_copilot.agent.planner import build_deterministic_plan_node
from erp_copilot.agent.state import AgentState, AgentStatus
from erp_copilot.application.events import append_run_event
from erp_copilot.domain.entities import Run, RunStep
from erp_copilot.domain.enums import StepStatus
from erp_copilot.memory.checkpoint import CheckpointSaver

logger = get_task_logger(__name__)

# Tool schemas the deterministic planner can emit (docs/05 simulator tools).
_TOOL_SCHEMAS: dict[str, ToolSpec] = {
    "getProductByName": ToolSpec(name="getProductByName", required_params=["name"]),
    "getProductById": ToolSpec(name="getProductById", required_params=["product_id"]),
    "getProductSubstitutesByName": ToolSpec(
        name="getProductSubstitutesByName", required_params=["name"]
    ),
    "querySuppliersByDeliveryRegion": ToolSpec(
        name="querySuppliersByDeliveryRegion", required_params=["region"]
    ),
    "getSupplierByStatus": ToolSpec(name="getSupplierByStatus", required_params=["status"]),
    "getOrderByOrderId": ToolSpec(name="getOrderByOrderId", required_params=["order_id"]),
    "createOrder": ToolSpec(
        name="createOrder",
        required_params=["product_id", "supplier_id", "quantity", "region"],
    ),
    "updateOrderStatus": ToolSpec(name="updateOrderStatus", required_params=["order_id", "status"]),
    "cancelOrder": ToolSpec(name="cancelOrder", required_params=["order_id"]),
}

# Runtime AgentStatus -> persisted Run.status. create_run and the pre-LangGraph
# worker use uppercase strings; the checkpoint layer separately stores the
# lowercase RunStatus view (map_agent_status) — the two lifecycles stay apart.
_RUN_STATUS_UPPER: dict[AgentStatus, str] = {
    AgentStatus.SUCCEEDED: "COMPLETED",
    AgentStatus.WAITING_APPROVAL: "WAITING_APPROVAL",
    AgentStatus.CANCELLED: "CANCELLED",
    AgentStatus.FAILED: "FAILED",
    AgentStatus.EXPIRED: "FAILED",
    # The single-pass worker cannot loop back into the graph, so the runtime
    # in-flight/retry states that exit the graph are terminal failures here.
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


def _default_query(product_name: str) -> str:
    """Compose the natural-language query the deterministic planner reads.

    The API only passes a product name; the query is what classify_intent and
    the planner operate on, so it is built to include the product name. Callers
    may override it (e.g. the two-step "查苹果库存并推荐供应商" demo) via the
    query task argument.
    """
    return f"查询{product_name}库存"


def _resolve_read_scopes(_tenant_id: str, _user_id: str) -> set[str]:
    """Scope resolver until RBAC lands.

    The worker's deterministic READ demo only grants read-only scopes. Write
    scopes are never granted here, so a future WRITE step stays gated (DENY,
    never a silent allow).
    """
    return {"product:read", "supplier:read"}


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


def _build_graph(checkpoint_saver: CheckpointSaver) -> CompiledStateGraph:
    """Build the worker graph with real nodes and the injected checkpoint saver."""
    return build_agent_graph(
        plan_node=build_deterministic_plan_node(),
        validate_node=build_validate_plan_node(tool_schemas=_TOOL_SCHEMAS),
        policy_node=build_policy_check_node(get_scopes=_resolve_read_scopes),
        execute_node=build_execute_steps_node(executor=erp_simulator_executor),
        verify_node=build_verify_results_node(),
        checkpoint_saver=checkpoint_saver,
    )


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
        result: dict = asyncio.run(graph.ainvoke(state_dict))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(lambda: asyncio.run(graph.ainvoke(state_dict))).result()

    return AgentState.model_validate(result)


def _persist(session, run: Run, final: AgentState) -> str:
    """Map the terminal AgentState onto the Run row and RunStep rows.

    The run row is re-read first so a cancel that landed while the graph ran
    is never clobbered: a settled CANCELLED run stays cancelled (the same
    settled-run rule failure_queue enforces for FAILED flips).
    """
    fresh = session.query(Run).filter_by(id=run.id).first()
    if fresh is None or fresh.status == "CANCELLED":
        return fresh.status if fresh is not None else run.status
    run = fresh
    run.status = _RUN_STATUS_UPPER[final.status]
    if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        run.completed_at = datetime.now(UTC)
    if run.status == "FAILED" and final.errors:
        first = final.errors[0]
        run.failure_code = first.code
        run.failure_reason = first.message
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

        state = AgentState(
            run_id=run.id,
            tenant_id=run.tenant_id,
            # No per-user identity is carried by create_run yet; the run executes
            # as a system actor so the policy gate resolves scopes (read-only).
            user_id=run.user_id or "system",
            query=query or _default_query(product_name),
            deadline_at=(
                datetime.now(UTC) + timedelta(seconds=deadline_s)
                if deadline_s is not None
                else None
            ),
        )
        graph = _build_graph(CheckpointSaver(session, event_sink=_make_status_event_sink(session)))
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
