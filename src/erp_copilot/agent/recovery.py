"""Worker crash recovery — task 5.8.

Implements docs/03 §7's recovery flow: a crashed worker restarts, loads the
latest checkpoint, reconciles it against the persisted idempotency records,
and marks every write step whose execution intent was recorded (a PENDING
idempotency record) but whose result is unknown as
RECOVERY_RECONCILIATION_REQUIRED instead of blindly retrying it — the
operation may already have taken effect at the external system. Completed
steps (a COMPLETED step result in the checkpoint, or a COMPLETED idempotency
record) are left untouched so they are never re-executed; READ steps are
naturally repeatable and never reconciled.

The module is pure orchestration: it loads and annotates state, it does not
persist. The worker feeds the reconciled state back into the graph and the
next checkpoint save persists the annotated errors.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from erp_copilot.agent.state import AgentState, StateError
from erp_copilot.domain.entities import IdempotencyRecord
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.memory.checkpoint import CheckpointSaver

RECOVERY_RECONCILIATION_REQUIRED = "RECOVERY_RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of one recovery load.

    state carries the reconciled AgentState (None when no checkpoint exists);
    resumed tells the caller whether the run can continue; reconciled_steps
    names the write steps flagged for reconciliation.
    """

    state: AgentState | None
    resumed: bool
    reconciled_steps: tuple[str, ...] = ()


class RunRecovery:
    """Load the latest checkpoint and reconcile it against write intents.

    The session is injected (same style as CheckpointSaver and
    IdempotencyStore) so the app passes the Postgres session factory's session
    and tests drive SQLite.
    """

    def __init__(
        self,
        session: Session,
        *,
        saver: CheckpointSaver | None = None,
    ) -> None:
        self._session = session
        self._saver = saver if saver is not None else CheckpointSaver(session)

    def load(self, run_id: str, tenant_id: str) -> RecoveryResult:
        """Resume *run_id* from its latest checkpoint, reconciled for writes."""
        state = self._saver.load_latest(run_id, tenant_id)
        if state is None:
            return RecoveryResult(state=None, resumed=False)
        reconciled = self._reconcile(state)
        return RecoveryResult(state=state, resumed=True, reconciled_steps=reconciled)

    def _reconcile(self, state: AgentState) -> tuple[str, ...]:
        """Flag write steps whose outcome is unknown as needing reconciliation.

        A step is left alone when the checkpoint already proves it succeeded
        (a COMPLETED step result) or the write provably settled: a COMPLETED
        idempotency record (success — the checkpoint merely lagged), a FAILED
        record (a definite, retryable failure), or no record at all (begin()
        never ran, so the tool never executed).
        """
        if state.plan is None:
            return ()
        reconciled: list[str] = []
        for step in state.plan.steps:
            if step.risk_level not in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
                continue
            if step.idempotency_key is None:
                continue
            result = state.step_results.get(step.step_id)
            if result is not None and result.status == StepStatus.COMPLETED:
                continue
            record = self._find_record(state.tenant_id, step.idempotency_key)
            if record is None or record.status != "PENDING":
                continue
            state.errors.append(
                StateError(
                    code=RECOVERY_RECONCILIATION_REQUIRED,
                    message=(
                        f"写步骤 {step.step_id} 执行状态不确定（幂等记录 PENDING），"
                        "需人工对账确认后再继续"
                    ),
                    step_id=step.step_id,
                    details={"idempotency_key": step.idempotency_key},
                )
            )
            reconciled.append(step.step_id)
        return tuple(reconciled)

    def _find_record(self, tenant_id: str, idempotency_key: str) -> IdempotencyRecord | None:
        return (
            self._session.query(IdempotencyRecord)
            .filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key)
            .first()
        )
