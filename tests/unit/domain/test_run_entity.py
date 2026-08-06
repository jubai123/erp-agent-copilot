"""Tests for Run, RunStep, and RunEvent models."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db


@pytest.fixture(scope="module")
def _init_db(ensure_test_database: str) -> None:
    import erp_copilot.domain.entities  # noqa: F401 - register models on Base

    settings = Settings(
        database_url=ensure_test_database,
        llm_api_key="sk-test",
    )
    init_db(settings)
    Base.metadata.create_all(get_engine())


@pytest.fixture(autouse=True)
def _clean_tables(_init_db: None) -> None:
    session = get_session()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(text(f"DELETE FROM {table.name} CASCADE"))
    session.commit()


class TestRun:
    """Acceptance: Run records agent execution with lifecycle states."""

    def test_create_run(self) -> None:
        from erp_copilot.domain.entities import Run, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-run")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Test Run")
        session.add(run)
        session.commit()

        assert run.id is not None
        assert run.tenant_id == tenant.id
        assert run.user_id == user.id
        assert run.title == "Test Run"

    def test_run_defaults_to_queued(self) -> None:
        from erp_copilot.domain.entities import Run, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-queued")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev2@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Q Run")
        session.add(run)
        session.commit()

        assert run.status == "QUEUED"

    def test_run_has_version_for_optimistic_lock(self) -> None:
        from erp_copilot.domain.entities import Run, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-version")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev3@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Version Test")
        session.add(run)
        session.commit()

        assert run.version == 1

    def test_run_lifecycle(self) -> None:
        from erp_copilot.domain.entities import Run, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-lifecycle")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev4@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Lifecycle")
        session.add(run)
        session.commit()

        run.status = "PLANNING"
        run.version += 1
        session.commit()

        run.status = "EXECUTING"
        run.version += 1
        session.commit()

        run.status = "COMPLETED"
        run.version += 1
        session.commit()

        session.refresh(run)
        assert run.status == "COMPLETED"
        assert run.version == 4


class TestRunStep:
    """Acceptance: Each step in a run records input, output, and timing."""

    def test_create_step_with_input_output(self) -> None:
        from erp_copilot.domain.entities import Run, RunStep, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-step")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev5@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Step Test")
        session.add(run)
        session.flush()

        step = RunStep(
            run_id=run.id,
            step_index=0,
            step_type="EXECUTE",
            input='{"action":"create_order"}',
            output='{"order_id":123}',
        )
        session.add(step)
        session.commit()

        assert step.id is not None
        assert step.run_id == run.id
        assert step.step_index == 0
        assert step.input == '{"action":"create_order"}'
        assert step.output == '{"order_id":123}'

    def test_step_has_timestamps(self) -> None:
        from datetime import UTC, datetime

        from erp_copilot.domain.entities import Run, RunStep, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-step-time")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev6@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Timing")
        session.add(run)
        session.flush()

        now = datetime.now(UTC)
        step = RunStep(
            run_id=run.id,
            step_index=0,
            step_type="EXECUTE",
            input="{}",
            started_at=now,
        )
        session.add(step)
        session.commit()

        assert step.started_at is not None
        assert step.completed_at is None

    def test_multiple_steps_ordering(self) -> None:
        from erp_copilot.domain.entities import Run, RunStep, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-ordering")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev7@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Ordered")
        session.add(run)
        session.flush()

        step0 = RunStep(run_id=run.id, step_index=0, step_type="PLAN", input="{}")
        step1 = RunStep(run_id=run.id, step_index=1, step_type="EXECUTE", input="{}")
        step2 = RunStep(run_id=run.id, step_index=2, step_type="VERIFY", input="{}")
        session.add_all([step0, step1, step2])
        session.commit()

        assert len(run.steps) == 3
        indices = sorted(s.step_index for s in run.steps)
        assert indices == [0, 1, 2]


class TestRunEvent:
    """Acceptance: Append-only event log with monotonic sequence numbers."""

    def test_create_event(self) -> None:
        from erp_copilot.domain.entities import Run, RunEvent, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-event")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev8@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Event")
        session.add(run)
        session.flush()

        event = RunEvent(run_id=run.id, sequence=0, event_type="STATUS_CHANGE", payload="{}")
        session.add(event)
        session.commit()

        assert event.id is not None
        assert event.run_id == run.id
        assert event.event_type == "STATUS_CHANGE"

    def test_events_are_append_only_ordered(self) -> None:
        from erp_copilot.domain.entities import Run, RunEvent, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-events-seq")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev9@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Append Only")
        session.add(run)
        session.flush()

        e0 = RunEvent(run_id=run.id, sequence=0, event_type="STATUS_CHANGE", payload="{}")
        e1 = RunEvent(run_id=run.id, sequence=1, event_type="STEP_START", payload="{}")
        e2 = RunEvent(run_id=run.id, sequence=2, event_type="STEP_COMPLETE", payload="{}")
        session.add_all([e0, e1, e2])
        session.commit()

        assert len(run.events) == 3
        sequences = sorted(e.sequence for e in run.events)
        assert sequences == [0, 1, 2]


class TestCascadeDelete:
    """Deleting a run removes all its steps and events."""

    def test_cascade_delete_run(self) -> None:
        from erp_copilot.domain.entities import Run, RunEvent, RunStep, Tenant, User

        session = get_session()

        tenant = Tenant(name="Acme", slug="acme-cascade-run")
        session.add(tenant)
        session.flush()

        user = User(tenant_id=tenant.id, email="dev10@acme.com", hashed_password="x")
        session.add(user)
        session.flush()

        run = Run(tenant_id=tenant.id, user_id=user.id, title="Cascade")
        session.add(run)
        session.flush()

        step = RunStep(run_id=run.id, step_index=0, step_type="EXECUTE", input="{}")
        event = RunEvent(run_id=run.id, sequence=0, event_type="LOG", payload="{}")
        session.add_all([step, event])
        session.commit()

        run_id = run.id
        step_id = step.id
        event_id = event.id

        session.delete(run)
        session.commit()

        assert session.get(Run, run_id) is None
        assert session.get(RunStep, step_id) is None
        assert session.get(RunEvent, event_id) is None
