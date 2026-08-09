"""Unit tests for the fault-injection harness — task 6.9.

The harness (``tests.performance.fault_injection``) models the three fault
scenarios from docs/08 §9 (故障注入和恢复) at the component boundary:

- ``worker_recovery`` — a worker "crashes" mid-execution. Because the worker
  task never re-invokes the agent graph, the crash is modelled as a fresh DB
  session (a fresh process) resuming from the checkpoint + idempotency rows
  the crashed session left behind.
- ``erp_timeout_retry`` — the ERP times out (504); after the operator heals
  the ERP the executor retries to 201, and the order is created exactly once.
- ``order_idempotency`` — POSTing the same idempotency key twice returns the
  same order_id and deducts stock exactly once.

Each negative test proves the harness is not vacuously green: a scenario
without its fault injected must report ``passed=False``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from apps.erp_simulator.simulator import create_simulator_app
from erp_copilot.agent.recovery import RunRecovery
from tests.performance.fault_injection import (
    RUN_ID,
    TENANT_ID,
    build_engine,
    inject_worker_crash,
    run_all,
    scenario_erp_timeout_retry,
    scenario_order_idempotency,
    scenario_worker_recovery,
)


class TestBuildEngine:
    def test_tables_created(self) -> None:
        engine = build_engine()
        inspector = inspect(engine)
        assert inspector.has_table("agent_checkpoints")
        assert inspector.has_table("idempotency_records")


class TestInjectWorkerCrash:
    def test_crash_is_resumable(self) -> None:
        engine = build_engine()
        inject_worker_crash(engine)
        result = RunRecovery(Session(engine)).load(RUN_ID, TENANT_ID)
        assert result.resumed is True
        assert result.reconciled_steps == ("s1",)


class TestScenarioWorkerRecovery:
    def test_after_crash_flags_s1_not_s2(self) -> None:
        engine = build_engine()
        inject_worker_crash(engine)
        result = scenario_worker_recovery(engine)
        assert result["passed"] is True
        assert "s1" in result["detail"]
        assert "s2" not in result["detail"]

    def test_missing_crash_is_not_vacuous(self) -> None:
        engine = build_engine()  # no crash injected
        result = scenario_worker_recovery(engine)
        assert result["passed"] is False
        assert "resumed" in result["detail"]


class TestScenarioErpTimeoutRetry:
    def test_504_retried_to_success(self) -> None:
        client = TestClient(create_simulator_app())
        client.put("/scenario/timeout")
        result = scenario_erp_timeout_retry(client)
        assert result["passed"] is True
        assert "[504, 201]" in result["detail"]
        assert "single_order=True" in result["detail"]

    def test_missing_timeout_is_not_vacuous(self) -> None:
        client = TestClient(create_simulator_app())
        client.put("/scenario/happy_path")  # no fault injected
        result = scenario_erp_timeout_retry(client)
        assert result["passed"] is False
        assert "expected [504, 201]" in result["detail"]


class TestScenarioOrderIdempotency:
    def test_same_key_creates_one_order(self) -> None:
        client = TestClient(create_simulator_app())
        result = scenario_order_idempotency(client)
        assert result["passed"] is True
        assert "same order=True" in result["detail"]


class TestRunAll:
    def test_all_scenarios_pass(self) -> None:
        report = run_all()
        assert report["total"] == 3
        assert report["passed"] == 3
        assert report["failures"] == []
