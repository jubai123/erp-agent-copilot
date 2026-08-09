"""Unit tests for agent/recovery.py — task 5.8.

Worker recovery (docs/03 §7): a crashed worker restarts, loads the latest
checkpoint, reconciles it against the persisted idempotency records, and marks
every write step whose execution intent was written (PENDING) but whose result
is unknown as requiring reconciliation — RECOVERY_RECONCILIATION_REQUIRED — so
it is not blindly retried. Steps the checkpoint already holds as COMPLETED, or
whose idempotency record is COMPLETED, are never re-run; READ steps are
naturally repeatable and are never reconciled.

Tests run against a shared SQLite file so the checkpoint and idempotency rows
written by the "crashed worker" session are visible to a fresh recovery
session, exactly as two processes would see them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from erp_copilot.agent.recovery import RunRecovery
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    Plan,
    PlanStep,
    StateError,
    StepResult,
)
from erp_copilot.domain.entities import AgentCheckpoint, IdempotencyRecord
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.memory.checkpoint import CheckpointSaver


@pytest.fixture()
def engine(tmp_path) -> Iterator[Engine]:
    """Shared SQLite file so separate sessions simulate two processes."""
    db = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    AgentCheckpoint.__table__.create(db)
    IdempotencyRecord.__table__.create(db)
    yield db
    db.dispose()


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def _state(**overrides: object) -> AgentState:
    data: dict[str, object] = {
        "run_id": "r1",
        "tenant_id": "t1",
        "query": "创建订单",
        "status": AgentStatus.EXECUTING,
        "plan": Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="createOrder",
                    risk_level=ToolRiskLevel.WRITE,
                    idempotency_key="k1",
                ),
                PlanStep(
                    step_id="s2",
                    tool_name="querySuppliers",
                    risk_level=ToolRiskLevel.READ,
                    idempotency_key="k2",
                ),
                PlanStep(
                    step_id="s3",
                    tool_name="cancelOrder",
                    risk_level=ToolRiskLevel.DANGEROUS,
                    idempotency_key="k3",
                ),
            ]
        ),
    }
    data.update(overrides)
    return AgentState(**data)


def _add_record(engine: Engine, *, key: str, step: str, status: str) -> None:
    with Session(engine) as db:
        db.add(
            IdempotencyRecord(
                tenant_id="t1",
                run_id="r1",
                step_id=step,
                idempotency_key=key,
                status=status,
                request_payload="{}",
            )
        )
        db.commit()


class TestLoad:
    def test_no_checkpoint_is_not_resumed(self, engine: Engine) -> None:
        result = RunRecovery(Session(engine)).load("r1", "t1")
        assert result.resumed is False
        assert result.state is None
        assert result.reconciled_steps == ()

    def test_loads_latest_checkpoint(self, engine: Engine) -> None:
        clock = _FakeClock()
        with Session(engine) as db:
            CheckpointSaver(db, clock=clock).save("execute_ready_steps", _state())
        with Session(engine) as db:
            CheckpointSaver(db, clock=clock).save(
                "verify_results", _state(status=AgentStatus.VERIFYING)
            )
        result = RunRecovery(Session(engine)).load("r1", "t1")
        assert result.resumed is True
        assert result.state is not None
        assert result.state.status == AgentStatus.VERIFYING  # B covers A

    def test_tenant_isolation(self, engine: Engine) -> None:
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", _state(tenant_id="t1"))
        result = RunRecovery(Session(engine)).load("r1", "t2")
        assert result.resumed is False  # never resumes another tenant's run
        assert result.state is None

    def test_no_plan_nothing_to_reconcile(self, engine: Engine) -> None:
        with Session(engine) as db:
            CheckpointSaver(db).save("build_plan", _state(plan=None))
        result = RunRecovery(Session(engine)).load("r1", "t1")
        assert result.resumed is True
        assert result.reconciled_steps == ()


class TestReconcile:
    def test_pending_write_step_marked_for_reconciliation(self, engine: Engine) -> None:
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", _state())
        _add_record(engine, key="k1", step="s1", status="PENDING")

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.reconciled_steps == ("s1",)
        assert result.state is not None
        errors = [e for e in result.state.errors if e.code == "RECOVERY_RECONCILIATION_REQUIRED"]
        assert len(errors) == 1
        assert errors[0].step_id == "s1"
        assert errors[0].details == {"idempotency_key": "k1"}

    def test_completed_idempotency_record_not_reconciled(self, engine: Engine) -> None:
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", _state())
        _add_record(engine, key="k1", step="s1", status="COMPLETED")

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.reconciled_steps == ()
        assert result.state is not None
        assert not [e for e in result.state.errors if e.code == "RECOVERY_RECONCILIATION_REQUIRED"]

    def test_failed_record_not_reconciled(self, engine: Engine) -> None:
        # A FAILED record is a definite outcome — retryable, not ambiguous.
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", _state())
        _add_record(engine, key="k1", step="s1", status="FAILED")

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.reconciled_steps == ()

    def test_read_step_never_reconciled(self, engine: Engine) -> None:
        # Reads are naturally repeatable — a PENDING read record is not ambiguous.
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", _state())
        _add_record(engine, key="k2", step="s2", status="PENDING")

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.reconciled_steps == ()

    def test_write_step_without_idempotency_key_skipped(self, engine: Engine) -> None:
        with Session(engine) as db:
            CheckpointSaver(db).save(
                "execute_ready_steps",
                _state(
                    plan=Plan(
                        steps=[
                            PlanStep(
                                step_id="s1",
                                tool_name="createOrder",
                                risk_level=ToolRiskLevel.WRITE,
                                idempotency_key=None,
                            )
                        ]
                    )
                ),
            )
        result = RunRecovery(Session(engine)).load("r1", "t1")
        assert result.reconciled_steps == ()

    def test_write_without_record_never_began_not_reconciled(self, engine: Engine) -> None:
        # No PENDING intent means begin() never ran, so the tool provably did
        # not execute — safe to run normally, nothing to reconcile.
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", _state())
        result = RunRecovery(Session(engine)).load("r1", "t1")
        assert result.reconciled_steps == ()

    def test_completed_step_result_wins_over_pending_record(self, engine: Engine) -> None:
        # The checkpoint already proves the step succeeded — trust it even if
        # the idempotency record is stale at PENDING.
        state = _state(
            step_results={
                "s1": StepResult(step_id="s1", status=StepStatus.COMPLETED, data={"id": 1001}),
            }
        )
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", state)
        _add_record(engine, key="k1", step="s1", status="PENDING")

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.reconciled_steps == ()


class TestStatePreservation:
    def test_completed_step_results_preserved_for_resume(self, engine: Engine) -> None:
        # execute_ready_steps skips steps that already hold a COMPLETED result,
        # so recovery must hand back the state unchanged for those steps.
        state = _state(
            step_results={
                "s1": StepResult(step_id="s1", status=StepStatus.COMPLETED, data={"id": 1001}),
            }
        )
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", state)

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.state is not None
        assert result.state.step_results["s1"].status == StepStatus.COMPLETED
        assert result.state.step_results["s1"].data == {"id": 1001}

    def test_recovery_appends_to_existing_errors(self, engine: Engine) -> None:
        state = _state(errors=[StateError(code="PREVIOUS", message="x")])
        with Session(engine) as db:
            CheckpointSaver(db).save("execute_ready_steps", state)
        _add_record(engine, key="k1", step="s1", status="PENDING")

        result = RunRecovery(Session(engine)).load("r1", "t1")

        assert result.state is not None
        assert [e.code for e in result.state.errors] == [
            "PREVIOUS",
            "RECOVERY_RECONCILIATION_REQUIRED",
        ]
