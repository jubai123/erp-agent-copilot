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
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import Run, RunEvent
from erp_copilot.domain.errors import CopilotError, NotFoundError

INVALID_RUN_TRANSITION = "INVALID_RUN_TRANSITION"
# A settled successful or cancelled run must never be flipped to FAILED.
_TERMINAL_REJECT = ("COMPLETED", "CANCELLED")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FailureQueue:
    """Record run failures with a reason + suggested action and query them."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session = session
        self._clock = clock

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

    def _next_sequence(self, run_id: str) -> int:
        max_seq = (
            self._session.query(func.max(RunEvent.sequence))
            .filter(RunEvent.run_id == run_id)
            .scalar()
        )
        return int(max_seq) + 1 if max_seq is not None else 0
