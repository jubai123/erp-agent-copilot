"""Failure queue and human intervention — task 5.9.

A failure that cannot auto-recover enters a human intervention queue: the run
transitions to FAILED and carries three diagnostic fields (docs/03 §9) — a
machine-readable ``error_code`` (e.g. PERMANENT_TOOL_ERROR,
RECOVERY_RECONCILIATION_REQUIRED), a human diagnosable ``failure_reason``, and
a ``suggested_action`` for the operator. The queue is simply the set of FAILED
runs with a recorded reason, queryable per tenant, most recently failed first.

Every record appends an immutable RUN_FAILED event (docs/03 §3: every state
change appends a run_event), so the failure detail is auditable independent of
the run row. The session is injected (same style as CheckpointSaver /
IdempotencyStore); the clock is an injectable seam so tests can order failures
deterministically. Run locking / CAS is the worker's concern (task 5.8) — this
service just refuses to flip a settled COMPLETED/CANCELLED run.

Resolve closes the reconciliation loop (docs/06 §8): an operator confirms what
a PENDING write actually did, resolve() finalizes the idempotency record
(COMPLETED when applied, FAILED when not), and requeues the run so the resumed
graph replays or retries it. A RUN_RESOLVED event is appended so the human
decision is auditable too.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import IdempotencyRecord, Run, RunEvent
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.memory.checkpoint import CheckpointSaver

INVALID_RUN_TRANSITION = "INVALID_RUN_TRANSITION"
NOT_IN_INTERVENTION_QUEUE = "NOT_IN_INTERVENTION_QUEUE"
STEP_NOT_FOUND = "STEP_NOT_FOUND"
# A settled successful or cancelled run must never be flipped to FAILED.
_TERMINAL_REJECT = ("COMPLETED", "CANCELLED")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ResolveDecision(StrEnum):
    """How a human settled one ambiguous write (docs/06 §8)."""

    CONFIRMED_APPLIED = "confirmed_applied"  # external system holds the write.
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"  # safe to retry.


class FailureQueue:
    """Record run failures with a reason + suggested action and query them."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] = _utcnow,
        saver: CheckpointSaver | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._saver = saver if saver is not None else CheckpointSaver(session)

    def record(
        self,
        *,
        run_id: str,
        tenant_id: str,
        error_code: str,
        reason: str,
        suggested_action: str,
    ) -> Run:
        """Transition *run_id* to FAILED, persisting the failure detail.

        The tenant filter keeps failure recording inside the tenant's boundary
        — one tenant can never fail another tenant's run. A COMPLETED or
        CANCELLED run is left untouched; an already-FAILED run is re-recorded
        with the fresh detail.
        """
        run = self._session.query(Run).filter_by(id=run_id, tenant_id=tenant_id).first()
        if run is None:
            raise NotFoundError(f"Run '{run_id}' not found in tenant '{tenant_id}'")
        if run.status in _TERMINAL_REJECT:
            raise CopilotError(
                f"Run '{run_id}' is already {run.status} and cannot be failed",
                code=INVALID_RUN_TRANSITION,
            )

        run.status = "FAILED"
        run.failure_code = error_code
        run.failure_reason = reason
        run.suggested_action = suggested_action
        run.completed_at = self._clock()
        run.version += 1
        self._session.add(
            RunEvent(
                run_id=run_id,
                sequence=self._next_sequence(run_id),
                event_type="RUN_FAILED",
                payload=json.dumps(
                    {
                        "error_code": error_code,
                        "failure_reason": reason,
                        "suggested_action": suggested_action,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        self._session.commit()
        return run

    def list_needing_intervention(self, tenant_id: str, *, limit: int = 50) -> list[Run]:
        """Return FAILED runs with a recorded reason, most recent first."""
        return (
            self._session.query(Run)
            .filter(
                Run.tenant_id == tenant_id,
                Run.status == "FAILED",
                Run.failure_reason.is_not(None),
            )
            .order_by(Run.completed_at.desc(), Run.id.desc())
            .limit(limit)
            .all()
        )

    def resolve(
        self,
        *,
        run_id: str,
        tenant_id: str,
        step_id: str,
        decision: ResolveDecision,
        decided_by: str,
        reason: str = "",
    ) -> Run:
        """Settle one ambiguous write and requeue the run for the worker.

        Only a run actually in the intervention queue (FAILED with a recorded
        reason) can be resolved. The step's idempotency key is read from the
        run's latest checkpoint plan — the same source the worker reconciles
        against — so the ledger row it names is settled: CONFIRMED_APPLIED
        finalizes it as COMPLETED (the resumed graph replays it), CONFIRMED_NOT_
        APPLIED marks it FAILED (the resumed graph safely retries). The run is
        then reset to QUEUED with its failure detail cleared and a RUN_RESOLVED
        event is appended (docs/03 §3: every state change appends a run_event).
        """
        run = self._session.query(Run).filter_by(id=run_id, tenant_id=tenant_id).first()
        if run is None:
            raise NotFoundError(f"Run '{run_id}' not found in tenant '{tenant_id}'")
        if run.status != "FAILED" or run.failure_reason is None:
            raise CopilotError(
                f"Run '{run_id}' is not in the intervention queue",
                code=NOT_IN_INTERVENTION_QUEUE,
            )

        state = self._saver.load_latest(run_id, tenant_id)
        step = None
        if state is not None and state.plan is not None:
            step = next((s for s in state.plan.steps if s.step_id == step_id), None)
        if step is None or step.idempotency_key is None:
            raise CopilotError(
                f"Step '{step_id}' not found in run '{run_id}'",
                code=STEP_NOT_FOUND,
            )
        record = (
            self._session.query(IdempotencyRecord)
            .filter_by(tenant_id=tenant_id, idempotency_key=step.idempotency_key)
            .first()
        )
        if record is None:
            raise CopilotError(
                f"Step '{step_id}' not found in run '{run_id}'",
                code=STEP_NOT_FOUND,
            )

        if decision is ResolveDecision.CONFIRMED_APPLIED:
            record.status = "COMPLETED"
            record.result_payload = "{}"
            record.error_message = None
        else:
            record.status = "FAILED"
            record.error_message = "人工确认外部系统无此操作，可安全重试"

        run.status = "QUEUED"
        run.failure_code = None
        run.failure_reason = None
        run.suggested_action = None
        run.completed_at = None
        run.version += 1
        self._session.add(
            RunEvent(
                run_id=run_id,
                sequence=self._next_sequence(run_id),
                event_type="RUN_RESOLVED",
                payload=json.dumps(
                    {
                        "step_id": step_id,
                        "decision": decision.value,
                        "decided_by": decided_by,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        self._session.commit()
        return run

    def _next_sequence(self, run_id: str) -> int:
        max_seq = (
            self._session.query(func.max(RunEvent.sequence))
            .filter(RunEvent.run_id == run_id)
            .scalar()
        )
        return int(max_seq) + 1 if max_seq is not None else 0
