"""Unit tests for memory/checkpoint.py — task 4.12.

The CheckpointSaver persists a full AgentState snapshot after every graph
node (state_json), so a crashed worker can load the latest checkpoint and
re-invoke the graph without re-executing completed tool steps. Tests run
against an in-memory SQLite database — the table schema is portable
(String/Text/DateTime), and the Postgres path is covered by integration
tests. Ordering determinism comes from an injected clock, since two saves
in the same process can otherwise share a microsecond.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    Plan,
    PlanStep,
    StateError,
    StepResult,
)
from erp_copilot.domain.entities import AgentCheckpoint
from erp_copilot.domain.enums import RunStatus, StepStatus
from erp_copilot.memory.checkpoint import CheckpointSaver, checkpointed, map_agent_status


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    # Only the checkpoint table: full Base.metadata carries Postgres-specific
    # columns (TSVECTOR, pgvector) that SQLite cannot compile.
    AgentCheckpoint.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 8, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def _state(**overrides: object) -> AgentState:
    data: dict[str, object] = {
        "run_id": "r1",
        "tenant_id": "t1",
        "user_id": "u1",
        "query": "查苹果",
        "status": AgentStatus.EXECUTING,
    }
    data.update(overrides)
    return AgentState(**data)


def _full_state() -> AgentState:
    return _state(
        plan=Plan(steps=[PlanStep(step_id="s1", tool_name="getProductById", arguments={"id": 1})]),
        step_results={
            "s1": StepResult(
                step_id="s1",
                status=StepStatus.COMPLETED,
                data={"name": "苹果"},
            )
        },
        errors=[StateError(code="PREVIOUS", message="x")],
    )


class TestMapAgentStatus:
    @pytest.mark.parametrize(
        ("agent", "expected"),
        [
            (AgentStatus.QUEUED, RunStatus.PENDING),
            (AgentStatus.PLANNING, RunStatus.PENDING),
            (AgentStatus.WAITING_APPROVAL, RunStatus.WAITING_APPROVAL),
            (AgentStatus.EXECUTING, RunStatus.RUNNING),
            (AgentStatus.VERIFYING, RunStatus.RUNNING),
            (AgentStatus.RETRYING, RunStatus.RUNNING),
            (AgentStatus.REPLANNING, RunStatus.RUNNING),
            (AgentStatus.SUCCEEDED, RunStatus.COMPLETED),
            (AgentStatus.FAILED, RunStatus.FAILED),
            (AgentStatus.EXPIRED, RunStatus.FAILED),
            (AgentStatus.CANCELLED, RunStatus.CANCELLED),
        ],
    )
    def test_agent_status_maps_to_run_status(self, agent: AgentStatus, expected: RunStatus) -> None:
        assert map_agent_status(agent) == expected


class TestCheckpointSaver:
    def test_round_trip_preserves_full_state(self, session: Session) -> None:
        CheckpointSaver(session).save("execute_ready_steps", _full_state())
        restored = CheckpointSaver(session).load_latest("r1", "t1")
        assert restored == _full_state()

    def test_load_latest_returns_most_recent_checkpoint(self, session: Session) -> None:
        saver = CheckpointSaver(session, clock=_FakeClock())
        saver.save("build_plan", _state(status=AgentStatus.PLANNING))
        saver.save("execute_ready_steps", _state(status=AgentStatus.EXECUTING))
        restored = saver.load_latest("r1", "t1")
        assert restored is not None
        assert restored.status == AgentStatus.EXECUTING

    def test_load_latest_breaks_created_at_ties_by_insertion_order(self, session: Session) -> None:
        # Two saves sharing one clock tick used to tie-break on the v4-UUID id
        # (random vs insertion order) and could return the stale row; the
        # insertion-ordered sequence must win regardless of created_at.
        at = datetime(2026, 8, 8, tzinfo=UTC)
        saver = CheckpointSaver(session, clock=lambda: at)
        saver.save("build_plan", _state(status=AgentStatus.PLANNING))
        saver.save("execute_ready_steps", _state(status=AgentStatus.EXECUTING))
        restored = saver.load_latest("r1", "t1")
        assert restored is not None
        assert restored.status == AgentStatus.EXECUTING

    def test_no_checkpoint_returns_none(self, session: Session) -> None:
        assert CheckpointSaver(session).load_latest("r1", "t1") is None

    def test_tenant_isolation(self, session: Session) -> None:
        CheckpointSaver(session).save("build_plan", _state(tenant_id="t1"))
        assert CheckpointSaver(session).load_latest("r1", "t2") is None

    def test_node_name_recorded(self, session: Session) -> None:
        CheckpointSaver(session).save("verify_results", _state())
        row = session.query(AgentCheckpoint).one()
        assert row.node_name == "verify_results"

    def test_run_status_column_is_mapped(self, session: Session) -> None:
        CheckpointSaver(session).save("execute_ready_steps", _state(status=AgentStatus.RETRYING))
        row = session.query(AgentCheckpoint).one()
        assert row.run_status == RunStatus.RUNNING


class _RecordingSaver:
    """Duck-typed stand-in for CheckpointSaver — the wrapper only calls save()."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, AgentState]] = []

    def save(self, node_name: str, state: AgentState) -> None:
        self.saved.append((node_name, state))


class TestCheckpointed:
    def test_wraps_sync_node_and_saves_merged_state(self) -> None:
        def node(state: AgentState) -> dict[str, object]:
            return {"status": AgentStatus.SUCCEEDED}

        saver = _RecordingSaver()
        wrapped = checkpointed("finalize", node, saver)  # type: ignore[arg-type]
        updates = wrapped(_state())
        assert updates["status"] == AgentStatus.SUCCEEDED
        assert saver.saved == [("finalize", _state(status=AgentStatus.SUCCEEDED))]

    def test_wraps_async_node(self) -> None:
        import asyncio

        async def node(state: AgentState) -> dict[str, object]:
            return {"status": AgentStatus.SUCCEEDED}

        saver = _RecordingSaver()
        wrapped = checkpointed("finalize", node, saver)  # type: ignore[arg-type]
        updates = asyncio.run(wrapped(_state()))
        assert updates["status"] == AgentStatus.SUCCEEDED
        assert saver.saved == [("finalize", _state(status=AgentStatus.SUCCEEDED))]


def test_map_agent_status_signature_is_callable() -> None:
    # Guards the pure-function seam used by the saver so a refactor cannot
    # silently change it into an unexported helper.
    assert callable(map_agent_status)
