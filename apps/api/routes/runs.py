"""Run creation, query, cancellation, and approval endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func

from apps.api.schemas.runs import ApproveRunRequest, CreateRunRequest, RunResponse
from erp_copilot.agent.graph import build_agent_graph
from erp_copilot.agent.state import AgentStatus, ApprovalStatus
from erp_copilot.domain.entities import Run, RunEvent
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.security.approval import decide_and_resume

router = APIRouter(prefix="/v1/runs", tags=["runs"])

# A run already settled (succeeded, failed, or cancelled) is never cancelled.
_CANCEL_REJECT: tuple[str, ...] = ("COMPLETED", "FAILED", "CANCELLED")


def _next_event_sequence(session, run_id: str) -> int:
    max_seq = session.query(func.max(RunEvent.sequence)).filter(RunEvent.run_id == run_id).scalar()
    return int(max_seq) + 1 if max_seq is not None else 0


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
        from apps.worker.tasks import execute_run

        execute_run.delay(run.id, body.product_name)
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
    step is skipped. The graph is built with the same builder the worker will
    use; real node injection lands with the worker wiring task.
    """
    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        decision = ApprovalStatus.APPROVED if body.decision == "APPROVE" else ApprovalStatus.DENIED
        saver = CheckpointSaver(session)
        decided, result = await decide_and_resume(
            build_agent_graph(checkpoint_saver=saver),
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
        session.add(
            RunEvent(
                run_id=run_id,
                sequence=_next_event_sequence(session, run_id),
                event_type="RUN_CANCELLED",
                payload=json.dumps({"status": "CANCELLED"}, ensure_ascii=False),
            )
        )
        session.commit()
        return {"run_id": run_id, "status": "CANCELLED"}
    finally:
        session.close()
