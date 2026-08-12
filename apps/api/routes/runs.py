"""Run creation, query, cancellation, approval, and event-stream endpoints."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from apps.api.schemas.runs import ApproveRunRequest, CreateRunRequest, RunResponse
from apps.worker.graph_builder import build_worker_graph
from erp_copilot.agent.state import AgentState, AgentStatus, ApprovalStatus
from erp_copilot.application.events import append_run_event
from erp_copilot.application.run_persistence import make_status_event_sink, persist_run
from erp_copilot.domain.entities import Run, RunEvent
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.security.approval import decide_and_resume

router = APIRouter(prefix="/v1/runs", tags=["runs"])

# A run already settled (succeeded, failed, or cancelled) is never cancelled.
_CANCEL_REJECT: tuple[str, ...] = ("COMPLETED", "FAILED", "CANCELLED")

# The SSE stream ends once the run reaches one of these terminal statuses.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


def _resume_sequence(session, run_id: str, last_event_id: str | None) -> int:
    """Return the sequence to resume from, or -1 to replay the whole log.

    A missing or unknown Last-Event-ID replays from the start — safer than
    dropping events the client never saw.
    """
    if not last_event_id:
        return -1
    row = session.query(RunEvent).filter(RunEvent.id == last_event_id).first()
    return row.sequence if row is not None else -1


def _run_is_terminal(session, run_id: str) -> bool:
    status = session.query(Run.status).filter_by(id=run_id).scalar()
    # A vanished run (row deleted mid-stream) closes the stream too.
    return status is None or status in _TERMINAL_STATUSES


async def _event_stream(run_id: str, resume_seq: int) -> AsyncIterator[str]:
    """Yield SSE frames for events after *resume_seq* until the run ends.

    Polls the append-only RunEvent log (docs/03 §3) rather than holding any
    in-memory channel, so a reconnecting client replays durable events and a
    disconnect only cancels this local generator.
    """
    session = get_session()
    try:
        seen = resume_seq
        last_heartbeat = time.monotonic()
        while True:
            rows = (
                session.query(RunEvent)
                .filter(RunEvent.run_id == run_id, RunEvent.sequence > seen)
                .order_by(RunEvent.sequence.asc())
                .all()
            )
            for row in rows:
                seen = row.sequence
                yield f"id: {row.id}\nevent: {row.event_type}\ndata: {row.payload}\n\n"
            if _run_is_terminal(session, run_id):
                return
            now = time.monotonic()
            if now - last_heartbeat >= 15.0:
                last_heartbeat = now
                yield ": ping\n\n"
            await asyncio.sleep(0.5)
    finally:
        session.close()


def _run_to_response(run: Run) -> RunResponse:
    return RunResponse(
        run_id=run.id,
        tenant_id=run.tenant_id,
        title=run.title or "",
        status=run.status,
        created_at=run.created_at,
    )


@router.post("", status_code=202)
async def create_run(body: CreateRunRequest, response: Response) -> RunResponse:
    """Create a new Run and dispatch it for execution. Returns 202 Accepted."""
    session = get_session()
    try:
        run = Run(
            tenant_id=body.tenant_id,
            title=body.title or "",
            status="QUEUED",
        )
        session.add(run)
        session.commit()
        append_run_event(session, run.id, "RUN_CREATED", {"status": AgentStatus.QUEUED.value})
        session.commit()
        from apps.worker.tasks import execute_run

        execute_run.delay(run.id, body.product_name, query=body.query)
        session.refresh(run)

        response.status_code = 202
        return _run_to_response(run)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.get("/{run_id}")
async def get_run(run_id: str) -> RunResponse:
    """Return the current status of a Run."""
    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return _run_to_response(run)
    finally:
        session.close()


@router.post("/{run_id}/approve")
async def approve_run(run_id: str, body: ApproveRunRequest, request: Request) -> dict[str, Any]:
    """Approve or deny a pending step and resume the run (docs/07 §8).

    The tenant boundary comes from the Run row, never from the client. The
    decision is flipped on the checkpoint, audited to audit_logs, then the
    graph resumes from the decided state — an approved step executes, a denied
    step is skipped. The graph is the shared worker graph (build_worker_graph,
    apps/worker/graph_builder), so a resumed write runs at-most-once through
    the idempotency store, and the terminal state is persisted onto the Run and
    RunStep rows (persist_run) — a FAILED/EXPIRED resume lands in the
    human-intervention queue.
    """
    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        decision = ApprovalStatus.APPROVED if body.decision == "APPROVE" else ApprovalStatus.DENIED
        saver = CheckpointSaver(session, event_sink=make_status_event_sink(session))
        decided, result = await decide_and_resume(
            build_worker_graph(session, saver),
            saver,
            session,
            run_id=run_id,
            tenant_id=run.tenant_id,
            step_id=body.step_id,
            decision=decision,
            decided_by=body.decided_by,
            reason=body.reason,
            ip=request.client.host if request.client else None,
            trace_id=body.trace_id,
        )
        persist_run(session, run, AgentState.model_validate(result))
        return {
            "run_id": run_id,
            "step_id": body.step_id,
            "decision": decision.value,
            "decided_by": body.decided_by,
            "decided_at": decided.decided_at.isoformat() if decided.decided_at else None,
            "run_status": result.get("status"),
        }
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except CopilotError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    finally:
        session.close()


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    """Cancel a run that has not reached a terminal state (task 4.15).

    The tenant boundary comes from the Run row, never from the client. A
    paused (WAITING_APPROVAL) run additionally gets a CANCELLED checkpoint so
    a later approve cannot resume it; a QUEUED run the worker has not picked
    up yet is protected by the worker refusing to process a CANCELLED run.
    Every cancel appends an immutable RUN_CANCELLED event (docs/03 §3).
    """
    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        if run.status in _CANCEL_REJECT:
            raise HTTPException(
                status_code=409,
                detail=f"Run '{run_id}' is already {run.status} and cannot be cancelled",
            )

        run.status = "CANCELLED"
        run.completed_at = datetime.now(UTC)
        saver = CheckpointSaver(session)
        state = saver.load_latest(run_id, run.tenant_id)
        if state is not None:
            saver.save(
                "cancel",
                state.model_copy(update={"status": AgentStatus.CANCELLED}),
            )
        else:
            session.commit()
        append_run_event(session, run_id, "RUN_CANCELLED", {"status": "CANCELLED"})
        session.commit()
        return {"run_id": run_id, "status": "CANCELLED"}
    finally:
        session.close()


@router.get("/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    """Stream the run's event log over SSE (task 4.14, docs/07 §12).

    Replays every RunEvent after the client's Last-Event-ID, then pushes new
    events as the worker records them, ending once the run reaches a terminal
    status. A disconnected client only cancels this local generator — the
    worker writes the database and never holds the connection.
    """
    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        resume_seq = _resume_sequence(session, run_id, request.headers.get("last-event-id"))
        return StreamingResponse(
            _event_stream(run_id, resume_seq),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    finally:
        session.close()
