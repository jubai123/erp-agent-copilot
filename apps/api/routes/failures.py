"""Human-intervention failure queue endpoints (task 5.9, write-path closure).

GET lists the tenant's queued failures (FAILED runs with a recorded reason,
most recent first); POST resolves one ambiguous write step — finalizing its
idempotency record per the operator's decision and requeuing the run — and
dispatches the worker so the run resumes from its checkpoint. The queue only
makes sense inside a tenant boundary, so list is scoped to the actor's tenant
and resolve additionally requires the order:write scope it exercises.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api.schemas.failures import (
    FailureItem,
    FailureListResponse,
    ResolveRunRequest,
    ResolveRunResponse,
)
from erp_copilot.application.failure_queue import FailureQueue
from erp_copilot.domain.entities import Run
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.infrastructure.database import get_session
from erp_copilot.security.dependencies import Actor, get_actor, require_run_tenant, require_scope

router = APIRouter(prefix="/v1/failures", tags=["failures"])


@router.get("", response_model=FailureListResponse)
def list_failures(actor: Actor = Depends(get_actor)) -> FailureListResponse:
    """Return the acting tenant's intervention queue, most recently failed first."""
    session = get_session()
    try:
        runs = FailureQueue(session).list_needing_intervention(actor.tenant_id)
        return FailureListResponse(
            items=[
                FailureItem(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    title=run.title or "",
                    failure_code=run.failure_code,
                    failure_reason=run.failure_reason,
                    suggested_action=run.suggested_action,
                    failed_at=run.completed_at,
                )
                for run in runs
            ]
        )
    finally:
        session.close()


@router.post("/{run_id}/resolve", response_model=ResolveRunResponse)
def resolve_failure(
    run_id: str,
    body: ResolveRunRequest,
    actor: Actor = Depends(require_scope("order:write")),
) -> ResolveRunResponse:
    """Settle one ambiguous write and requeue the run for the worker.

    Only a run actually in the intervention queue can be resolved; the step's
    idempotency record is finalized per the operator's decision (confirmed
    applied -> COMPLETED, so a resumed graph replays it; confirmed not applied
    -> FAILED, so it safely retries). The run is reset to QUEUED and the worker
    is dispatched — its recovery path loads the checkpoint and resumes, reading
    the settled record instead of re-running the tool blind.
    """
    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        require_run_tenant(run.tenant_id, actor)

        updated = FailureQueue(session).resolve(
            run_id=run_id,
            tenant_id=actor.tenant_id,
            step_id=body.step_id,
            decision=body.decision,
            decided_by=body.decided_by,
            reason=body.reason or "",
        )
        # Read the requeued status before dispatching: the resolve commit
        # expires the instance, and the worker re-runs the graph from the
        # checkpoint asynchronously — the response must report what resolve did
        # (requeue), not whatever the resumed run reaches later.
        status = updated.status
        from apps.worker.tasks import execute_run

        execute_run.delay(run_id)
        return ResolveRunResponse(
            run_id=run_id,
            step_id=body.step_id,
            decision=body.decision.value,
            decided_by=body.decided_by,
            status=status,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except CopilotError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    finally:
        session.close()
