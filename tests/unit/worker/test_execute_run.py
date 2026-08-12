"""Unit tests for apps/worker/tasks.py execute_run — task 4.16.

The worker drives the full LangGraph (build_agent_graph with real nodes and a
CheckpointSaver bound to the DB session) instead of the pre-graph direct
simulator shortcut. These tests run the task against an in-memory SQLite
session and assert the Run lifecycle, the RunStep rows written from the
terminal state, the per-node checkpoints, and the preserved simulator
scenarios (timeout / stock_insufficient / unconstructable plan).

The task's DB session is patched to the fixture's SQLite engine; the
DATABASE_URL placeholder above only satisfies Settings() at import time and is
never connected to.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator

# Settings() (triggered by apps.worker.celery_app) requires DATABASE_URL. The
# env file already provides one; a placeholder is only a backstop so the module
# imports even when the env file is absent. The fixture patches the task's DB
# session to an in-memory SQLite engine — nothing connects to this URL.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from apps.erp_simulator.data.orders import get_by_idempotency_key  # noqa: E402
from apps.erp_simulator.data.products import PRODUCT_BY_NAME  # noqa: E402
from apps.worker.graph_builder import build_worker_graph  # noqa: E402
from apps.worker.tasks import execute_run  # noqa: E402
from erp_copilot.agent.state import AgentState, AgentStatus, ApprovalStatus  # noqa: E402
from erp_copilot.application.failure_queue import FailureQueue  # noqa: E402
from erp_copilot.application.run_persistence import persist_run  # noqa: E402
from erp_copilot.domain.entities import (  # noqa: E402
    AgentCheckpoint,
    IdempotencyRecord,
    Role,
    RoleScope,
    Run,
    RunEvent,
    RunStep,
    Tenant,
    User,
    UserRole,
)
from erp_copilot.memory.checkpoint import CheckpointSaver  # noqa: E402
from erp_copilot.observability.metrics import METRICS, generate_latest  # noqa: E402
from erp_copilot.security.approval import (  # noqa: E402
    ApprovalDecisionService,
    resume_run,
)

_EXPECTED_NODE_ORDER: set[str] = {
    "check_deadline",
    "classify_intent",
    "retrieve_context",
    "build_plan",
    "validate_plan",
    "policy_check",
    "request_approval",
    "execute_ready_steps",
    "verify_results",
    "finalize",
}


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """In-memory SQLite engine shared by the fixture and the task's sessions.

    StaticPool keeps a single shared connection so the row seeded by the
    fixture and the rows written by execute_run's own session live in the same
    database (the :memory: URL otherwise gives each connection its own DB).
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Tenant.__table__.create(engine)
    User.__table__.create(engine)
    Role.__table__.create(engine)
    RoleScope.__table__.create(engine)
    UserRole.__table__.create(engine)
    Run.__table__.create(engine)
    RunStep.__table__.create(engine)
    AgentCheckpoint.__table__.create(engine)
    RunEvent.__table__.create(engine)
    IdempotencyRecord.__table__.create(engine)

    monkeypatch.setattr("erp_copilot.infrastructure.database.get_session", lambda: Session(engine))
    with Session(engine) as db:
        yield db
    engine.dispose()


def _make_tenant(session: Session) -> Tenant:
    tenant = Tenant(name="worker-test", slug="worker-test")
    session.add(tenant)
    session.commit()
    return tenant


def _make_run(
    session: Session, tenant_id: str, title: str = "查询库存", user_id: str | None = None
) -> Run:
    run = Run(tenant_id=tenant_id, title=title, status="QUEUED", user_id=user_id)
    session.add(run)
    session.commit()
    return run


def _make_user(session: Session, tenant_id: str, scopes: list[tuple[str, str]]) -> str:
    """Create an active user bound to a role granting *scopes*; return its id.

    DB-driven RBAC (docs/06 §4) resolves the acting user's scopes from the role
    graph, so a test that expects real tool execution must provision a user
    holding the plan's required scopes — otherwise every scoped step is
    policy-denied and the run completes as a silent no-op.
    """
    role = Role(tenant_id=tenant_id, name="tester")
    session.add(role)
    session.flush()
    for resource, action in scopes:
        session.add(RoleScope(role_id=role.id, resource=resource, action=action))
    user = User(
        tenant_id=tenant_id,
        email=f"user-{role.id}@example.com",
        hashed_password="x",
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user.id


def _make_reader_user(session: Session, tenant_id: str) -> str:
    """Return a user holding the READ scopes the planner stamps on read steps."""
    return _make_user(session, tenant_id, [("product", "read"), ("supplier", "read")])


def _make_writer_user(session: Session, tenant_id: str) -> str:
    """Return a user whose scopes let a create-order DAG pause for approval."""
    return _make_user(
        session,
        tenant_id,
        [("product", "read"), ("supplier", "read"), ("order", "write")],
    )


class TestHappyPath:
    def test_product_query_completes_and_persists_step(self, session: Session) -> None:
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)

        result = execute_run(run.id, "苹果")

        assert result["status"] == "COMPLETED"
        session.refresh(run)
        assert run.status == "COMPLETED"
        assert run.completed_at is not None
        assert len(run.steps) == 1
        step = run.steps[0]
        assert step.status == "COMPLETED"
        assert step.step_type == "TOOL_CALL"
        assert json.loads(step.input) == {"name": "苹果"}
        data = json.loads(step.output)
        assert data["name"] == "苹果"
        # The executor surfaces the simulator's live stock; an order-placement
        # test elsewhere in the suite may have decremented the shared product
        # dict, so compare against the in-memory value rather than a constant.
        assert data["stock"] == PRODUCT_BY_NAME["苹果"].quantity_in_stock

    def test_stock_and_supplier_query_runs_two_read_steps(self, session: Session) -> None:
        # task 4.16 acceptance: "查苹果库存并推荐供应商" -> two READ steps.
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)

        result = execute_run(run.id, "苹果", query="查苹果库存并推荐供应商")

        assert result["status"] == "COMPLETED"
        session.refresh(run)
        assert len(run.steps) == 2
        assert all(step.status == "COMPLETED" for step in run.steps)
        inputs = [json.loads(step.input) for step in run.steps]
        assert {"name": "苹果"} in inputs
        assert {"status": "AVAILABLE"} in inputs

    def test_checkpoint_rows_written_for_every_node(self, session: Session) -> None:
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)

        execute_run(run.id, "苹果")

        rows = session.query(AgentCheckpoint).filter_by(run_id=run.id).all()
        assert len(rows) == 10
        assert {row.node_name for row in rows} == _EXPECTED_NODE_ORDER
        finalize_row = next(row for row in rows if row.node_name == "finalize")
        assert finalize_row.run_status == "completed"

    def test_lifecycle_events_recorded_in_order(self, session: Session) -> None:
        # task 4.14: the worker's status-event sink records one RUN_STATUS per
        # runtime status change (deduped), so the SSE stream can replay
        # planning -> executing -> succeeded without per-node noise.
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)

        execute_run(run.id, "苹果")

        events = (
            session.query(RunEvent).filter_by(run_id=run.id).order_by(RunEvent.sequence.asc()).all()
        )
        assert events
        assert events[0].event_type == "RUN_STATUS"
        assert events[0].payload
        assert json.loads(events[0].payload)["status"] == "planning"
        statuses = [
            json.loads(event.payload)["status"]
            for event in events
            if event.event_type == "RUN_STATUS"
        ]
        assert statuses == ["planning", "executing", "succeeded"]


class TestScenarios:
    def test_timeout_scenario_fails_run(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import apps.erp_simulator.scenarios as _scenarios

        monkeypatch.setattr(_scenarios, "_current_scenario", "timeout")
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)

        result = execute_run(run.id, "苹果")

        assert result["status"] == "FAILED"
        session.refresh(run)
        assert run.status == "FAILED"
        assert run.steps[0].status == "FAILED"
        assert run.failure_code == "TIMEOUT"

    def test_stock_insufficient_completes_with_zero_stock(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import apps.erp_simulator.scenarios as _scenarios

        monkeypatch.setattr(_scenarios, "_current_scenario", "stock_insufficient")
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)

        result = execute_run(run.id, "苹果")

        assert result["status"] == "COMPLETED"
        session.refresh(run)
        assert json.loads(run.steps[0].output)["stock"] == 0

    def test_query_without_product_fails_with_empty_plan(self, session: Session) -> None:
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)

        result = execute_run(run.id, "苹果", query="查询库存")

        assert result["status"] == "FAILED"
        session.refresh(run)
        assert run.status == "FAILED"
        assert run.failure_code == "EMPTY_PLAN"


class TestDeadlineAndCancel:
    """Task 4.15: deadline expiry and cancellation guards on the worker path."""

    def test_expired_deadline_fails_run_with_deadline_code(self, session: Session) -> None:
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)

        result = execute_run(run.id, "苹果", deadline_s=-1)

        assert result["status"] == "FAILED"
        session.refresh(run)
        assert run.status == "FAILED"
        assert run.failure_code == "DEADLINE_EXCEEDED"
        assert run.steps == []

    def test_cancelled_run_is_not_reprocessed(self, session: Session) -> None:
        tenant = _make_tenant(session)
        run = Run(tenant_id=tenant.id, title="已取消", status="CANCELLED")
        session.add(run)
        session.commit()

        result = execute_run(run.id, "苹果")

        assert result["status"] == "CANCELLED"
        session.refresh(run)
        assert run.status == "CANCELLED"
        assert run.steps == []
        assert run.completed_at is None

    def test_persist_does_not_overwrite_cancelled_run(self, session: Session) -> None:
        # A cancel that landed mid-execution (another session flipped the row
        # while the graph ran) must survive the worker's persist: the guard
        # re-reads the row and refuses to clobber a settled CANCELLED run.
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)
        run.status = "CANCELLED"
        session.commit()

        final = AgentState(
            run_id=run.id,
            tenant_id=tenant.id,
            query="查询苹果库存",
            status=AgentStatus.SUCCEEDED,
        )
        status = persist_run(session, run, final)

        assert status == "CANCELLED"
        session.refresh(run)
        assert run.status == "CANCELLED"
        assert run.steps == []


class TestWritePathEndToEnd:
    """Task 4.17: worker grants order:write so a WRITE run pauses for approval,
    then resumes through the real graph with at-most-once idempotency.

    Before the grant, a create-order query is DENY-gated at policy_check and the
    run "completes" with the write silently skipped. After the grant the run
    pauses in WAITING_APPROVAL, and approving step s3 resumes the graph so the
    order lands exactly once (idempotency key f"{run.id}:s3").
    """

    def test_write_run_pauses_then_resume_executes(self, session: Session) -> None:
        tenant = _make_tenant(session)
        user_id = _make_writer_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)
        product = PRODUCT_BY_NAME["苹果"]
        original_stock = product.quantity_in_stock
        try:
            result = execute_run(run.id, "苹果", query="帮我在上海下一单 1 KG 苹果")

            assert result["status"] == "WAITING_APPROVAL"
            session.refresh(run)
            assert run.status == "WAITING_APPROVAL"

            saver = CheckpointSaver(session)
            ApprovalDecisionService(saver).decide(
                run_id=run.id,
                tenant_id=tenant.id,
                step_id="s3",
                decision=ApprovalStatus.APPROVED,
                decided_by="tester",
            )

            graph = build_worker_graph(session, saver)
            final = asyncio.run(resume_run(graph, saver, run_id=run.id, tenant_id=tenant.id))

            assert final["status"] == "succeeded"
            s3 = final["step_results"]["s3"]
            assert s3.status == "completed"
            assert s3.data["status"] == "CREATED"
            assert s3.data["quantity"] == 1

            order = get_by_idempotency_key(f"{run.id}:s3")
            assert order is not None
            assert order.status == "CREATED"
            assert order.quantity == 1

            record = (
                session.query(IdempotencyRecord).filter_by(idempotency_key=f"{run.id}:s3").first()
            )
            assert record is not None
            assert record.status == "COMPLETED"
        finally:
            product.quantity_in_stock = original_stock


class TestRecoveryAndFailureQueue:
    """Task 5.8/5.9 worker wiring: crash-resume from checkpoint + FAILED -> human queue."""

    def test_crash_resume_replays_decided_approval_and_executes(self, session: Session) -> None:
        # A run paused for approval, approved, then re-entered (worker crashed
        # before resuming) must resume from the checkpoint — not restart blank
        # and lose the decision. Without RunRecovery the second execute_run
        # would re-pause in WAITING_APPROVAL forever.
        tenant = _make_tenant(session)
        user_id = _make_writer_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)
        product = PRODUCT_BY_NAME["苹果"]
        original_stock = product.quantity_in_stock
        try:
            first = execute_run(run.id, "苹果", query="帮我在上海下一单 1 KG 苹果")
            assert first["status"] == "WAITING_APPROVAL"

            ApprovalDecisionService(CheckpointSaver(session)).decide(
                run_id=run.id,
                tenant_id=tenant.id,
                step_id="s3",
                decision=ApprovalStatus.APPROVED,
                decided_by="tester",
            )

            second = execute_run(run.id, "苹果", query="帮我在上海下一单 1 KG 苹果")

            assert second["status"] == "COMPLETED"
            session.refresh(run)
            assert run.status == "COMPLETED"
            order = get_by_idempotency_key(f"{run.id}:s3")
            assert order is not None
            assert order.status == "CREATED"
            record = (
                session.query(IdempotencyRecord).filter_by(idempotency_key=f"{run.id}:s3").first()
            )
            assert record is not None
            assert record.status == "COMPLETED"
        finally:
            product.quantity_in_stock = original_stock

    def test_failed_run_records_suggested_action_and_enters_human_queue(
        self, session: Session
    ) -> None:
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)

        result = execute_run(run.id, "苹果", query="查询库存")

        assert result["status"] == "FAILED"
        session.refresh(run)
        assert run.status == "FAILED"
        assert run.failure_code == "EMPTY_PLAN"
        assert run.failure_reason
        assert run.suggested_action
        event = session.query(RunEvent).filter_by(run_id=run.id, event_type="RUN_FAILED").first()
        assert event is not None
        payload = json.loads(event.payload)
        assert payload["error_code"] == "EMPTY_PLAN"
        assert payload["suggested_action"] == run.suggested_action
        queue = FailureQueue(session).list_needing_intervention(tenant.id)
        assert [q.id for q in queue] == [run.id]

    def test_reconciliation_uncertain_write_enters_human_queue(self, session: Session) -> None:
        # Crash between begin() and complete(): the idempotency record stays
        # PENDING, so the write outcome is unknown. On resume RunRecovery flags
        # the step and the run fails into the human queue instead of an unsafe
        # auto-retry.
        tenant = _make_tenant(session)
        user_id = _make_writer_user(session, tenant.id)
        run = _make_run(session, tenant.id, user_id=user_id)
        product = PRODUCT_BY_NAME["苹果"]
        original_stock = product.quantity_in_stock
        try:
            execute_run(run.id, "苹果", query="帮我在上海下一单 1 KG 苹果")
            ApprovalDecisionService(CheckpointSaver(session)).decide(
                run_id=run.id,
                tenant_id=tenant.id,
                step_id="s3",
                decision=ApprovalStatus.APPROVED,
                decided_by="tester",
            )
            session.add(
                IdempotencyRecord(
                    tenant_id=tenant.id,
                    run_id=run.id,
                    step_id="s3",
                    idempotency_key=f"{run.id}:s3",
                    status="PENDING",
                    request_payload="{}",
                )
            )
            session.commit()

            result = execute_run(run.id, "苹果", query="帮我在上海下一单 1 KG 苹果")

            assert result["status"] == "FAILED"
            session.refresh(run)
            assert run.status == "FAILED"
            assert run.failure_code == "RECOVERY_RECONCILIATION_REQUIRED"
            queue = FailureQueue(session).list_needing_intervention(tenant.id)
            assert [q.id for q in queue] == [run.id]
        finally:
            product.quantity_in_stock = original_stock


def _counter(name: str) -> float:
    """Read a cumulative counter from the shared module registry (delta reads)."""
    for line in generate_latest(METRICS).splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[-1])
    return 0.0


class TestCrashBranchFailedMetric:
    """Session 18 known gap: a graph crash bypasses persist_run and marks the
    run FAILED directly in execute_run's except branch, so the failed counter
    must be bumped there too — the crash path is the only failure that never
    reaches the persist_run counter. The branch must mirror persist_run and
    FailureQueue's settled-run guard: a run already CANCELLED/COMPLETED/FAILED
    when the crash lands is never flipped to FAILED and never counted."""

    def test_graph_crash_marks_failed_and_increments_failed_counter(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("simulated graph crash")

        monkeypatch.setattr("apps.worker.tasks._invoke_graph", boom)
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)
        failed_before = _counter("erp_runs_failed_total")

        with pytest.raises(RuntimeError, match="simulated graph crash"):
            execute_run(run.id, "苹果")

        session.refresh(run)
        assert run.status == "FAILED"
        assert run.completed_at is not None
        assert _counter("erp_runs_failed_total") == failed_before + 1

    def test_crash_does_not_overwrite_cancelled_run_or_count_it(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Race: the run was mid-graph when the cancel endpoint landed CANCELLED
        # (committed) and the graph then crashed. The crash branch must treat a
        # settled run like persist_run/FailureQueue do — leave it alone.
        def cancel_then_crash(*args: object, **kwargs: object) -> object:
            row = session.query(Run).filter_by(id=run.id).first()
            assert row is not None
            row.status = "CANCELLED"
            session.commit()
            raise RuntimeError("simulated graph crash")

        monkeypatch.setattr("apps.worker.tasks._invoke_graph", cancel_then_crash)
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)
        failed_before = _counter("erp_runs_failed_total")

        with pytest.raises(RuntimeError, match="simulated graph crash"):
            execute_run(run.id, "苹果")

        session.refresh(run)
        assert run.status == "CANCELLED"
        assert _counter("erp_runs_failed_total") == failed_before
