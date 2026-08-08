"""Unit tests for tools/idempotency.py — task 5.6.

The idempotency store guarantees a write operation executes at most once per
key (docs/06 §7): it writes an execution intent (PENDING) before running the
tool, records the result on success, and replays a completed record from the
cache instead of re-running. The unique (tenant_id, idempotency_key)
constraint is what makes replay safe under concurrency. Tests use a shared
SQLite file so cache behaviour can be verified across two independent sessions
— proving the record really persisted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import IdempotencyRecord
from erp_copilot.domain.errors import IdempotencyConflictError
from erp_copilot.tools.idempotency import IdempotencyStore


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """Shared SQLite file so multiple sessions see the same data."""
    db = create_engine(f"sqlite:///{tmp_path / 'idempotency.db'}")
    IdempotencyRecord.__table__.create(db)
    return db


def _execute(
    engine: Engine,
    *,
    key: str,
    fn,
    tenant: str = "t1",
    run: str = "r1",
    step: str = "s1",
    external_operation_id: str | None = None,
) -> object:
    with Session(engine) as db:
        return IdempotencyStore(db).execute(
            tenant_id=tenant,
            run_id=run,
            step_id=step,
            idempotency_key=key,
            request_payload='{"q": "创建订单"}',
            fn=fn,
            external_operation_id=external_operation_id,
        )


class TestExecuteCaching:
    """Same key returns the cached result; the tool runs only once."""

    def test_same_key_runs_once_and_caches(self, engine: Engine) -> None:
        calls: list[str] = []

        def create_order() -> str:
            calls.append("x")
            return "OK:order-1001"

        first = _execute(engine, key="k1", fn=create_order)
        second = _execute(engine, key="k1", fn=create_order)

        assert first.cached is False
        assert second.cached is True
        assert len(calls) == 1  # the tool did not run again
        assert first.result == second.result == "OK:order-1001"

    def test_cached_result_matches_first_run(self, engine: Engine) -> None:
        first = _execute(engine, key="k1", fn=lambda: "OUT-1")
        second = _execute(engine, key="k1", fn=lambda: "OUT-2")
        assert first.result == "OUT-1"
        assert second.result == "OUT-1"  # cache, not the new value


class TestDifferentKeys:
    def test_different_keys_execute_independently(self, engine: Engine) -> None:
        calls: list[str] = []

        def fn() -> str:
            calls.append("x")
            return "OK"

        assert _execute(engine, key="k1", fn=fn).cached is False
        assert _execute(engine, key="k2", fn=fn).cached is False
        assert len(calls) == 2

        with Session(engine) as db:
            rows = db.query(IdempotencyRecord).all()
        assert {row.idempotency_key for row in rows} == {"k1", "k2"}


class TestPersistence:
    def test_record_persists_all_fields(self, engine: Engine) -> None:
        _execute(engine, key="k1", fn=lambda: "OK:order-1001", external_operation_id="ERP-1001")

        with Session(engine) as db:
            row = db.query(IdempotencyRecord).filter_by(idempotency_key="k1").one()
        assert row.tenant_id == "t1"
        assert row.run_id == "r1"
        assert row.step_id == "s1"
        assert row.status == "COMPLETED"
        assert row.request_payload == '{"q": "创建订单"}'
        assert row.result_payload == "OK:order-1001"
        assert row.external_operation_id == "ERP-1001"

    def test_cache_survives_a_fresh_session(self, engine: Engine) -> None:
        # First session writes and commits the record.
        calls: list[str] = []

        def fn() -> str:
            calls.append("x")
            return "OK:order-2002"

        _execute(engine, key="k1", fn=fn)

        # A brand-new session replays the same key from the database.
        replay = _execute(engine, key="k1", fn=fn)
        assert replay.cached is True
        assert replay.result == "OK:order-2002"
        assert len(calls) == 1  # the tool never ran a second time


class TestConcurrencyAndRetry:
    def test_inflight_pending_raises_conflict(self, engine: Engine) -> None:
        # A previous process began the operation but never finished it.
        with Session(engine) as db:
            db.add(
                IdempotencyRecord(
                    tenant_id="t1",
                    run_id="r1",
                    step_id="s1",
                    idempotency_key="k1",
                    status="PENDING",
                    request_payload="{}",
                )
            )
            db.commit()

        with pytest.raises(IdempotencyConflictError):
            _execute(engine, key="k1", fn=lambda: "OK")

    def test_failed_record_is_retried_on_next_attempt(self, engine: Engine) -> None:
        def flaky() -> str:
            if not hasattr(flaky, "ran"):
                flaky.ran = True  # type: ignore[attr-defined]
                raise RuntimeError("connection reset")
            return "OK:order-3003"

        with pytest.raises(RuntimeError):
            _execute(engine, key="k1", fn=flaky)

        with Session(engine) as db:
            failed = db.query(IdempotencyRecord).filter_by(idempotency_key="k1").one()
        assert failed.status == "FAILED"
        assert failed.error_message == "connection reset"

        retry = _execute(engine, key="k1", fn=flaky)
        assert retry.cached is False
        assert retry.result == "OK:order-3003"

        with Session(engine) as db:
            row = db.query(IdempotencyRecord).filter_by(idempotency_key="k1").one()
        assert row.status == "COMPLETED"
        assert row.error_message is None


class TestExecutionIntent:
    def test_begin_writes_pending_intent(self, engine: Engine) -> None:
        with Session(engine) as db:
            store = IdempotencyStore(db)
            record = store.begin(
                tenant_id="t1",
                run_id="r1",
                step_id="s1",
                idempotency_key="k1",
                request_payload='{"q": "创建订单"}',
            )
            assert record.status == "PENDING"
            assert record.id  # materialised
            again = store.begin(
                tenant_id="t1",
                run_id="r1",
                step_id="s1",
                idempotency_key="k1",
                request_payload='{"q": "创建订单"}',
            )
        assert again.id == record.id  # same record, not a duplicate
