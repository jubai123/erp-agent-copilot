"""Idempotent write execution — task 5.6.

Guarantees a write operation runs at most once per idempotency key (docs/06
§7): an execution intent (PENDING) is recorded *before* the tool runs, the
result lands on success, and a later attempt at the same key replays the
cached result instead of re-running. The unique ``(tenant_id, idempotency_key)``
constraint on idempotency_records is what makes replay safe under concurrency —
two processes racing on the same key collide in the database, not in
application logic.

The store takes a :class:`sqlalchemy.orm.Session` (same injection style as
:class:`erp_copilot.memory.checkpoint.CheckpointSaver`) so tests drive a real
transaction boundary and the executor wires it with its own session. Only write
steps carry an idempotency key — reads are naturally repeatable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import IdempotencyRecord
from erp_copilot.domain.errors import IdempotencyConflictError


@dataclass(frozen=True)
class IdempotentResult:
    """Outcome of one idempotent execution attempt.

    cached is True when the record was already COMPLETED and its result is
    being replayed; result carries either that replay or the fresh output.
    """

    cached: bool
    result: str | None
    record: IdempotencyRecord


class IdempotencyStore:
    """At-most-once execution over an idempotency_records ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # Ids of PENDING records this store created. A PENDING record we own is
        # our own in-progress operation (idempotent re-entry); one we did not
        # create belongs to another process and is an in-flight conflict.
        self._created_ids: set[str] = set()

    def begin(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_payload: str,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> IdempotencyRecord:
        """Record the execution intent, or return the existing record.

        A COMPLETED record is returned so the caller can replay its result; a
        FAILED record is returned so the caller may retry. A PENDING record
        created by another process means the operation is already in flight and
        raises a conflict. If two processes race past the lookup, the unique
        constraint turns the second insert into an IntegrityError, converted to
        the same conflict.
        """
        record = self._lookup(tenant_id, idempotency_key)
        if record is not None:
            if record.status == "PENDING" and record.id not in self._created_ids:
                raise IdempotencyConflictError(
                    f"Write with idempotency key '{idempotency_key}' is already in flight",
                )
            return record

        record = IdempotencyRecord(
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=step_id,
            idempotency_key=idempotency_key,
            status="PENDING",
            request_payload=request_payload,
        )
        self._session.add(record)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._lookup(tenant_id, idempotency_key)
            if existing is None or existing.status == "PENDING":
                raise IdempotencyConflictError(
                    f"Write with idempotency key '{idempotency_key}' is already in flight",
                ) from None
            return existing
        self._created_ids.add(record.id)
        return record

    def complete(
        self,
        record: IdempotencyRecord,
        result_payload: str,
        *,
        external_operation_id: str | None = None,
    ) -> IdempotencyRecord:
        """Mark the operation done and record its output for future replays."""
        record.status = "COMPLETED"
        record.result_payload = result_payload
        if external_operation_id is not None:
            record.external_operation_id = external_operation_id
        record.error_message = None
        self._session.commit()
        return record

    def fail(self, record: IdempotencyRecord, error_message: str) -> IdempotencyRecord:
        """Record a failed attempt so recovery can decide to retry or abort."""
        record.status = "FAILED"
        record.error_message = error_message
        self._session.commit()
        return record

    def execute(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_payload: str,
        fn: Callable[[], str],
        run_id: str | None = None,
        step_id: str | None = None,
        external_operation_id: str | None = None,
    ) -> IdempotentResult:
        """Run *fn* exactly once per key; replay returns the cached result.

        The intent is recorded first (docs/06 §7 rule 2), then the tool runs,
        then the result is recorded (rule 3). A COMPLETED record short-circuits
        to its stored result without calling *fn*; a FAILED record is retried.
        """
        record = self.begin(
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=step_id,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if record.status == "COMPLETED":
            return IdempotentResult(cached=True, result=record.result_payload, record=record)

        try:
            result = fn()
        except Exception as exc:
            self.fail(record, str(exc))
            raise
        self.complete(record, result, external_operation_id=external_operation_id)
        return IdempotentResult(cached=False, result=result, record=record)

    def _lookup(self, tenant_id: str, idempotency_key: str) -> IdempotencyRecord | None:
        return (
            self._session.query(IdempotencyRecord)
            .filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key)
            .first()
        )
