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

from apps.erp_simulator.data.products import PRODUCT_BY_NAME  # noqa: E402
from apps.worker.tasks import execute_run  # noqa: E402
from erp_copilot.domain.entities import AgentCheckpoint, Run, RunStep, Tenant  # noqa: E402

_EXPECTED_NODE_ORDER: set[str] = {
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
    Run.__table__.create(engine)
    RunStep.__table__.create(engine)
    AgentCheckpoint.__table__.create(engine)

    monkeypatch.setattr("erp_copilot.infrastructure.database.get_session", lambda: Session(engine))
    with Session(engine) as db:
        yield db
    engine.dispose()


def _make_tenant(session: Session) -> Tenant:
    tenant = Tenant(name="worker-test", slug="worker-test")
    session.add(tenant)
    session.commit()
    return tenant


def _make_run(session: Session, tenant_id: str, title: str = "查询库存") -> Run:
    run = Run(tenant_id=tenant_id, title=title, status="QUEUED")
    session.add(run)
    session.commit()
    return run


class TestHappyPath:
    def test_product_query_completes_and_persists_step(self, session: Session) -> None:
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)

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
        run = _make_run(session, tenant.id)

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
        run = _make_run(session, tenant.id)

        execute_run(run.id, "苹果")

        rows = session.query(AgentCheckpoint).filter_by(run_id=run.id).all()
        assert len(rows) == 9
        assert {row.node_name for row in rows} == _EXPECTED_NODE_ORDER
        finalize_row = next(row for row in rows if row.node_name == "finalize")
        assert finalize_row.run_status == "completed"


class TestScenarios:
    def test_timeout_scenario_fails_run(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import apps.erp_simulator.scenarios as _scenarios

        monkeypatch.setattr(_scenarios, "_current_scenario", "timeout")
        tenant = _make_tenant(session)
        run = _make_run(session, tenant.id)

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
        run = _make_run(session, tenant.id)

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
