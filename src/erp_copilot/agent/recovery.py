"""Worker crash recovery — task 5.8 + active reconciliation (write-path closure).

Implements docs/03 §7's recovery flow: a crashed worker restarts, loads the
latest checkpoint, reconciles it against the persisted idempotency records,
and settles every write step whose execution intent was recorded (a PENDING
idempotency record) but whose result is unknown instead of blindly retrying
it — the operation may already have taken effect at the external system.

Docs/06 §8 lets recovery "查询外部操作结果或转人工": with a reconciler injected,
a PENDING write is first checked against the external ERP (getOrderByOrderId on
the recorded external_operation_id) — APPLIED finalizes the ledger as COMPLETED
so a resumed graph replays it, NOT_APPLIED marks it FAILED so a resumed graph
safely retries, and only UNKNOWN (no handle, or a query that failed) keeps the
step flagged RECOVERY_RECONCILIATION_REQUIRED for a human. Without a reconciler
every unresolved write goes to the human path, preserving the pre-write-path
behaviour. Completed steps (a COMPLETED step result in the checkpoint, or a
COMPLETED idempotency record) are left untouched so they are never re-executed;
READ steps are naturally repeatable and never reconciled.

Active reconciliation is the one place this module persists: settling a record
updates idempotency_records (status + payload). Checkpoint state is otherwise
only annotated, never written here — the worker feeds the reconciled state back
into the graph and the next checkpoint save persists the annotations.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from erp_copilot.agent.state import AgentState, StateError
from erp_copilot.domain.entities import IdempotencyRecord
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.tools.tool_result import ToolResult

RECOVERY_RECONCILIATION_REQUIRED = "RECOVERY_RECONCILIATION_REQUIRED"


class ReconciliationOutcome(StrEnum):
    """What checking the external ERP found about one write (docs/06 §8)."""

    APPLIED = "APPLIED"  # external system holds the operation — never re-run.
    NOT_APPLIED = "NOT_APPLIED"  # external system has no trace — safe to retry.
    UNKNOWN = "UNKNOWN"  # could not be queried or gave no clear answer.


@dataclass(frozen=True)
class Reconciliation:
    """Outcome of one active reconciliation, with the external result payload.

    result_payload is only meaningful for APPLIED: it is persisted onto the
    idempotency record so a resumed graph replays the outcome through
    IdempotencyStore without re-invoking the tool.
    """

    outcome: ReconciliationOutcome
    result_payload: str | None = None


# A reconciler settles one PENDING write record. It is synchronous so recovery
# stays a plain call path; build_write_reconciler adapts the async executor.
WriteReconciler = Callable[[IdempotencyRecord], Reconciliation]


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
        reconciler: WriteReconciler | None = None,
    ) -> None:
        self._session = session
        self._saver = saver if saver is not None else CheckpointSaver(session)
        self._reconciler = reconciler

    def load(self, run_id: str, tenant_id: str) -> RecoveryResult:
        """Resume *run_id* from its latest checkpoint, reconciled for writes."""
        state = self._saver.load_latest(run_id, tenant_id)
        if state is None:
            return RecoveryResult(state=None, resumed=False)
        reconciled = self._reconcile(state)
        return RecoveryResult(state=state, resumed=True, reconciled_steps=reconciled)

    def _reconcile(self, state: AgentState) -> tuple[str, ...]:
        """Settle write steps whose outcome is unknown, routing only the rest to
        human reconciliation.

        A step is left alone when the checkpoint already proves it succeeded
        (a COMPLETED step result) or the write provably settled: a COMPLETED
        idempotency record (success — the checkpoint merely lagged), a FAILED
        record (a definite, retryable failure), or no record at all (begin()
        never ran, so the tool never executed).

        For a PENDING record the injected reconciler (when present) checks the
        external system first: APPLIED and NOT_APPLIED settle the ledger and are
        not flagged; only UNKNOWN falls through to the human path below.
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
            if self._reconciler is not None:
                settlement = self._reconciler(record)
                if settlement.outcome is ReconciliationOutcome.APPLIED:
                    self._settle_applied(record, settlement.result_payload)
                    continue
                if settlement.outcome is ReconciliationOutcome.NOT_APPLIED:
                    self._settle_not_applied(record)
                    continue
                # UNKNOWN falls through to the human path below.
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

    def _settle_applied(self, record: IdempotencyRecord, result_payload: str | None) -> None:
        """Finalize the ledger as COMPLETED once the external system confirmed it.

        The persisted payload lets a resumed graph replay the outcome through
        IdempotencyStore without re-invoking the tool (which a lost-response
        retry against a cloud ERP without idempotency keys could otherwise
        duplicate).
        """
        record.status = "COMPLETED"
        record.result_payload = result_payload or "{}"
        record.error_message = None
        self._session.commit()

    def _settle_not_applied(self, record: IdempotencyRecord) -> None:
        """Mark the record FAILED: the external system proves the write never
        ran, so the resumed graph may safely retry it."""
        record.status = "FAILED"
        record.error_message = "对账确认外部系统无此操作，可安全重试"
        self._session.commit()

    def _find_record(self, tenant_id: str, idempotency_key: str) -> IdempotencyRecord | None:
        return (
            self._session.query(IdempotencyRecord)
            .filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key)
            .first()
        )


def build_write_reconciler(
    executor: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
) -> WriteReconciler:
    """Build a reconciler that reads the external ERP for the operation id.

    Only records carrying an external_operation_id can be checked (the planner
    stamps idempotency keys, but only createOrder returns an order id today);
    a record without one is UNKNOWN and stays on the human path. The async
    executor is adapted to a sync call the same way the worker runs the graph:
    asyncio.run when no loop is running, else a dedicated thread's loop.
    """

    def reconcile(record: IdempotencyRecord) -> Reconciliation:
        if record.external_operation_id is None:
            return Reconciliation(ReconciliationOutcome.UNKNOWN)
        result = _run_sync(
            executor("getOrderByOrderId", {"order_id": record.external_operation_id})
        )
        if result.status == "SUCCEEDED" and isinstance(result.data, dict):
            return Reconciliation(
                ReconciliationOutcome.APPLIED,
                result_payload=json.dumps(result.data, ensure_ascii=False),
            )
        if result.error is not None and result.error.error_code == "ORDER_NOT_FOUND":
            return Reconciliation(ReconciliationOutcome.NOT_APPLIED)
        return Reconciliation(ReconciliationOutcome.UNKNOWN)

    return reconcile


async def _await_it(coro: Awaitable[ToolResult]) -> ToolResult:
    return await coro


def _run_sync(coro: Awaitable[ToolResult]) -> ToolResult:
    """Run one async executor call to completion on the caller's thread.

    Mirrors apps/worker/tasks._invoke_graph: a running loop (Celery eager mode
    inside FastAPI) gets a dedicated thread with its own loop; a normal prefork
    worker has none and runs inline. _await_it adapts the Awaitable to the
    Coroutine asyncio.run requires.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_it(coro))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_await_it(coro))).result()
