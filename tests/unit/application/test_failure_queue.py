"""Unit tests for application/failure_queue.py — task 5.9.

Failures that cannot auto-recover enter a human intervention queue: the run
transitions to FAILED and carries a machine-readable error code, a human
diagnosable reason, and a suggested action (docs/03 §3 requires every state
change to append an immutable run_event, so the failure is also audited).
The queue is the set of FAILED runs with a recorded reason, queryable per
tenant, most-recently-failed first.

The service is tested against an in-memory SQLite database with just the
runs and run_events tables created — tenant_id is a plain column here, so no
tenants/users rows are needed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from erp_copilot.application.failure_queue import FailureQueue
from erp_copilot.domain.entities import Run, RunEvent
from erp_copilot.domain.errors import CopilotError, NotFoundError


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Run.__table__.create(engine)
    RunEvent.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(minutes=1)
        return self._now


def _run(
    session: Session, *, tenant: str = "t1", status: str = "EXECUTING", **kwargs: object
) -> Run:
    run = Run(tenant_id=tenant, title="订单创建", status=status, **kwargs)
    session.add(run)
    session.commit()
    return run


class TestRecord:
    def test_marks_run_failed_with_diagnosable_detail(self, session: Session) -> None:
        run = _run(session)
        before = run.version

        updated = FailureQueue(session).record(
            run_id=run.id,
            tenant_id="t1",
            error_code="PERMANENT_TOOL_ERROR",
            reason="ERP 返回 422 业务拒绝",
            suggested_action="检查参数或联系 ERP 管理员",
        )

        assert updated.status == "FAILED"
        assert updated.failure_code == "PERMANENT_TOOL_ERROR"
        assert updated.failure_reason == "ERP 返回 422 业务拒绝"
        assert updated.suggested_action == "检查参数或联系 ERP 管理员"
        assert updated.completed_at is not None
        assert updated.version == before + 1

    def test_missing_run_raises_not_found(self, session: Session) -> None:
        with pytest.raises(NotFoundError):
            FailureQueue(session).record(
                run_id="nope",
                tenant_id="t1",
                error_code="PERMANENT_TOOL_ERROR",
                reason="x",
                suggested_action="y",
            )

    def test_tenant_isolation(self, session: Session) -> None:
        run = _run(session, tenant="t1")
        with pytest.raises(NotFoundError):
            FailureQueue(session).record(
                run_id=run.id,
                tenant_id="t2",  # another tenant cannot fail t1's run
                error_code="PERMANENT_TOOL_ERROR",
                reason="x",
                suggested_action="y",
            )

    def test_refuses_to_flip_completed_run(self, session: Session) -> None:
        run = _run(session, status="COMPLETED")
        with pytest.raises(CopilotError) as exc:
            FailureQueue(session).record(
                run_id=run.id,
                tenant_id="t1",
                error_code="PERMANENT_TOOL_ERROR",
                reason="x",
                suggested_action="y",
            )
        assert exc.value.code == "INVALID_RUN_TRANSITION"

    def test_refuses_to_flip_cancelled_run(self, session: Session) -> None:
        run = _run(session, status="CANCELLED")
        with pytest.raises(CopilotError):
            FailureQueue(session).record(
                run_id=run.id,
                tenant_id="t1",
                error_code="PERMANENT_TOOL_ERROR",
                reason="x",
                suggested_action="y",
            )

    def test_appends_run_failed_event_with_payload(self, session: Session) -> None:
        run = _run(session)
        FailureQueue(session).record(
            run_id=run.id,
            tenant_id="t1",
            error_code="PERMANENT_TOOL_ERROR",
            reason="ERP 超时",
            suggested_action="稍后重试",
        )

        events = session.query(RunEvent).filter_by(run_id=run.id).all()
        assert len(events) == 1
        assert events[0].event_type == "RUN_FAILED"
        assert events[0].sequence == 0
        payload = json.loads(events[0].payload)
        assert payload["error_code"] == "PERMANENT_TOOL_ERROR"
        assert payload["failure_reason"] == "ERP 超时"
        assert payload["suggested_action"] == "稍后重试"

    def test_event_sequence_increments_across_records(self, session: Session) -> None:
        run = _run(session)
        queue = FailureQueue(session)
        queue.record(
            run_id=run.id, tenant_id="t1", error_code="A", reason="r1", suggested_action="a1"
        )
        queue.record(
            run_id=run.id, tenant_id="t1", error_code="B", reason="r2", suggested_action="a2"
        )
        sequences = [e.sequence for e in session.query(RunEvent).filter_by(run_id=run.id).all()]
        assert sequences == [0, 1]


class TestReconciliation:
    def test_reconciliation_failure_marked_for_intervention(self, session: Session) -> None:
        run = _run(session)
        queue = FailureQueue(session)
        queue.record(
            run_id=run.id,
            tenant_id="t1",
            error_code="RECOVERY_RECONCILIATION_REQUIRED",
            reason="写步骤 s1 执行状态不确定",
            suggested_action="人工对账幂等记录后再继续",
        )

        pending = queue.list_needing_intervention("t1")
        assert [r.id for r in pending] == [run.id]
        assert pending[0].failure_code == "RECOVERY_RECONCILIATION_REQUIRED"
        assert pending[0].suggested_action == "人工对账幂等记录后再继续"


class TestListNeedingIntervention:
    def test_returns_only_failed_runs_with_reason(self, session: Session) -> None:
        queue = FailureQueue(session)
        failed = _run(
            session,
            status="FAILED",
            failure_code="X",
            failure_reason="boom",
            suggested_action="fix",
        )
        _run(session, status="COMPLETED")
        _run(session, status="EXECUTING")
        _run(session, status="FAILED")  # failed without a reason — not in the queue

        assert [r.id for r in queue.list_needing_intervention("t1")] == [failed.id]

    def test_tenant_scoped(self, session: Session) -> None:
        queue = FailureQueue(session)
        failed_t1 = _run(session, tenant="t1", status="FAILED", failure_reason="boom")
        _run(session, tenant="t2", status="FAILED", failure_reason="boom")

        assert [r.id for r in queue.list_needing_intervention("t1")] == [failed_t1.id]

    def test_orders_most_recently_failed_first(self, session: Session) -> None:
        queue = FailureQueue(session, clock=_FakeClock())
        first = _run(session, status="RUNNING")
        second = _run(session, status="RUNNING")
        queue.record(
            run_id=first.id, tenant_id="t1", error_code="A", reason="r1", suggested_action="a1"
        )
        queue.record(
            run_id=second.id, tenant_id="t1", error_code="B", reason="r2", suggested_action="a2"
        )

        assert [r.id for r in queue.list_needing_intervention("t1")] == [second.id, first.id]
