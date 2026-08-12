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
from datetime import UTC, datetime, timedelta

from celery import Task
from celery.utils.log import get_task_logger
from langgraph.graph.state import CompiledStateGraph

from apps.worker.celery_app import celery_app
from apps.worker.graph_builder import build_worker_graph
from erp_copilot.agent.recovery import RunRecovery
from erp_copilot.agent.state import AgentState
from erp_copilot.application.run_persistence import make_status_event_sink, persist_run
from erp_copilot.domain.entities import Run
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.observability.metrics import METRICS

logger = get_task_logger(__name__)

# The worst retry/replan sequence (2 retries -> 1 replan -> give up) visits
# ~24 nodes, close to LangGraph's default recursion_limit of 25; raise it so a
# budgeted recovery loop never trips GraphRecursionError.
_RECURSION_LIMIT = 50


def _default_query(product_name: str) -> str:
    """Compose the natural-language query the deterministic planner reads.

    The API only passes a product name; the query is what classify_intent and
    the planner operate on, so it is built to include the product name. Callers
    may override it (e.g. the two-step "查苹果库存并推荐供应商" demo) via the
    query task argument.
    """
    return f"查询{product_name}库存"


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

        saver = CheckpointSaver(session, event_sink=make_status_event_sink(session))
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
                # The acting user's scopes resolve from the role graph via
                # resolve_user_scopes; None (system-initiated) resolves to no
                # scopes, so every scoped step (reads included) is policy-denied
                # and the run completes without executing.
                user_id=run.user_id,
                query=query or _default_query(product_name),
                deadline_at=(
                    datetime.now(UTC) + timedelta(seconds=deadline_s)
                    if deadline_s is not None
                    else None
                ),
            )
        final = _invoke_graph(graph, state)
        status = persist_run(session, run, final)
        logger.info("Run %s finished with status %s", run_id, status)
        return {"status": status, "run_id": run.id}
    except Exception:
        session.rollback()
        try:
            run = session.query(Run).filter_by(id=run_id).first()
            # A run settled (cancelled/completed) before the crash landed stays
            # as-is, mirroring persist_run's fresh-status guard — flipping it to
            # FAILED would clobber a user cancel and miscount it as a failure.
            if run and run.status not in {"CANCELLED", "COMPLETED", "FAILED"}:
                run.status = "FAILED"
                run.completed_at = datetime.now(UTC)
                session.commit()
                # The crash bypassed persist_run (it is what took us down), so
                # this direct FAILED write is the one failure never counted by
                # persist_run — bump the counter only after the commit lands.
                METRICS.runs_failed.inc()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()
