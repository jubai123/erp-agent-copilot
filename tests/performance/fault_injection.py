"""Fault-injection and recovery harness — task 6.9.

Models docs/08 §9's three fault scenarios (Worker 被 kill / ERP 超时 / 订单
创建不重复) deterministically, with no live Postgres, Redis, or LLM:

- ``scenario_worker_recovery`` — a worker "crashes" mid-execution. The real
  worker task never re-invokes the agent graph (apps/worker/tasks.py), so the
  crash is modelled at the component boundary: a fresh SQLite engine is a
  fresh process. ``inject_worker_crash`` writes a checkpoint plus idempotency
  rows the way a crashed session would leave them; recovery loads the latest
  checkpoint and must flag exactly the step whose write intent is PENDING
  (outcome unknown) for reconciliation, never the COMPLETED one.
- ``scenario_erp_timeout_retry`` — the ERP times out (504, transient). After
  the operator heals the ERP, ``RetryExecutor`` retries to 201; the same
  idempotency key afterwards returns 200 with the same order_id, so the order
  is created exactly once.
- ``scenario_order_idempotency`` — POSTing the same key twice returns the same
  order_id and deducts stock exactly once.

The simulator's scenario/order/product state is a process global shared across
``TestClient`` instances, so every scenario uses a unique uuid idempotency key
and checks relative stock deltas. Each scenario restores ``happy_path``, and
the caller arranges the fault it wants to inject — that is what lets the
negative tests prove the harness is not vacuously green.

Run standalone::

    uv run python tests/performance/fault_injection.py
"""

from __future__ import annotations

import uuid
from typing import TypedDict

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.erp_simulator.simulator import create_simulator_app
from erp_copilot.agent.recovery import RECOVERY_RECONCILIATION_REQUIRED, RunRecovery
from erp_copilot.agent.retry_policy import RetryExecutor
from erp_copilot.agent.state import AgentState, AgentStatus, Plan, PlanStep
from erp_copilot.domain.entities import AgentCheckpoint, IdempotencyRecord
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.tools.tool_result import ToolResult

RUN_ID = "fault-run-1"
TENANT_ID = "fault-tenant-1"

_ORDER_PRODUCT_ID = 1
_ORDER_SUPPLIER_ID = 3
_ORDER_REGION = "上海"


class ScenarioResult(TypedDict):
    name: str
    passed: bool
    detail: str


class Report(TypedDict):
    total: int
    passed: int
    scenarios: list[ScenarioResult]
    failures: list[ScenarioResult]


def build_engine() -> Engine:
    """In-memory SQLite shared across sessions = one process's durable state."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AgentCheckpoint.__table__.create(engine)
    IdempotencyRecord.__table__.create(engine)
    return engine


def inject_worker_crash(
    engine: Engine, *, run_id: str = RUN_ID, tenant_id: str = TENANT_ID
) -> None:
    """Persist state as if the worker died between write steps.

    s1's write intent is PENDING (outcome unknown — must be reconciled) while
    s2's is COMPLETED (provably settled — must never be re-run).
    """
    state = AgentState(
        run_id=run_id,
        tenant_id=tenant_id,
        query="创建订单并更新库存",
        status=AgentStatus.EXECUTING,
        plan=Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="createOrder",
                    risk_level=ToolRiskLevel.WRITE,
                    idempotency_key="k1",
                ),
                PlanStep(
                    step_id="s2",
                    tool_name="updateStock",
                    risk_level=ToolRiskLevel.WRITE,
                    idempotency_key="k2",
                ),
            ]
        ),
    )
    with Session(engine) as db:
        CheckpointSaver(db).save("execute_ready_steps", state)
    with Session(engine) as db:
        db.add(
            IdempotencyRecord(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id="s1",
                idempotency_key="k1",
                status="PENDING",
                request_payload="{}",
            )
        )
        db.add(
            IdempotencyRecord(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id="s2",
                idempotency_key="k2",
                status="COMPLETED",
                request_payload="{}",
            )
        )
        db.commit()


def scenario_worker_recovery(engine: Engine) -> ScenarioResult:
    """The crashed worker's successor must resume and reconcile only s1."""
    result = RunRecovery(Session(engine)).load(RUN_ID, TENANT_ID)
    if not result.resumed:
        return ScenarioResult(
            name="worker_recovery", passed=False, detail="not resumed (no checkpoint)"
        )
    state = result.state
    flagged = (
        {e.step_id for e in state.errors if e.code == RECOVERY_RECONCILIATION_REQUIRED}
        if state is not None
        else set()
    )
    reconciled = set(result.reconciled_steps)
    ok = "s1" in reconciled and "s2" not in reconciled and "s1" in flagged and "s2" not in flagged
    if ok:
        return ScenarioResult(
            name="worker_recovery",
            passed=True,
            detail="resumed=True, reconciled=['s1'], flagged=['s1']",
        )
    return ScenarioResult(
        name="worker_recovery",
        passed=False,
        detail=(
            "resumed=True, "
            f"reconciled={sorted(reconciled)}, flagged={sorted(flagged)}, expected ['s1']"
        ),
    )


def scenario_erp_timeout_retry(client: TestClient) -> ScenarioResult:
    """A 504 must be retried (not fail the run) and never duplicate the order.

    The caller arranges the fault: with ``/scenario/timeout`` active the first
    call returns 504 and must not touch stock; after the operator heals the ERP
    the executor retries to 201. A healthy ERP (no fault) makes the first call
    already succeed, so the harness reports passed=False.
    """
    key = f"fault-timeout-{uuid.uuid4().hex}"
    payload = {
        "product_id": _ORDER_PRODUCT_ID,
        "quantity": 2,
        "supplier_id": _ORDER_SUPPLIER_ID,
        "region": _ORDER_REGION,
        "idempotency_key": key,
    }
    attempts: list[int] = []

    def create_order() -> ToolResult:
        resp = client.post("/orders", json=payload)
        attempts.append(resp.status_code)
        if resp.status_code == 504:
            return ToolResult.failure("v1", "TIMEOUT", "ERP timeout", is_retryable=True)
        if resp.status_code in (200, 201):
            return ToolResult.success("v1", resp.json())
        return ToolResult.failure(
            "v1", "HTTP_ERROR", f"unexpected {resp.status_code}", is_retryable=False
        )

    stock_before = client.get("/products/1/stock").json()["stock"]
    probe = create_order()
    stock_after_timeout = client.get("/products/1/stock").json()["stock"]
    client.put("/scenario/happy_path")  # operator heals the ERP
    final = RetryExecutor(sleep=lambda _: None).execute(create_order, max_retries=3)

    repeat = client.post("/orders", json=payload)
    timeout_had_no_effect = probe.error is not None and stock_after_timeout == stock_before
    single_order = (
        final.status == "SUCCEEDED"
        and repeat.status_code == 200
        and final.data is not None
        and final.data.get("order_id") == repeat.json().get("order_id")
    )
    if attempts == [504, 201] and timeout_had_no_effect and single_order:
        return ScenarioResult(
            name="erp_timeout_retry",
            passed=True,
            detail="attempts=[504, 201], single_order=True, timeout had no effect",
        )
    return ScenarioResult(
        name="erp_timeout_retry",
        passed=False,
        detail=(
            f"attempts={attempts}, expected [504, 201], "
            f"single_order={single_order}, timeout_had_no_effect={timeout_had_no_effect}"
        ),
    )


def scenario_order_idempotency(client: TestClient) -> ScenarioResult:
    """Same key twice must yield one order and one stock deduction."""
    client.put("/scenario/happy_path")
    key = f"fault-idem-{uuid.uuid4().hex}"
    payload = {
        "product_id": _ORDER_PRODUCT_ID,
        "quantity": 3,
        "supplier_id": _ORDER_SUPPLIER_ID,
        "region": _ORDER_REGION,
        "idempotency_key": key,
    }
    stock_before = client.get("/products/1/stock").json()["stock"]
    first = client.post("/orders", json=payload)
    second = client.post("/orders", json=payload)
    stock_after = client.get("/products/1/stock").json()["stock"]
    same_order = (
        first.status_code == 201
        and second.status_code == 200
        and second.json()["order_id"] == first.json()["order_id"]
    )
    deducted_once = stock_before - stock_after == payload["quantity"]
    if same_order and deducted_once:
        return ScenarioResult(
            name="order_idempotency",
            passed=True,
            detail=f"same order=True, stock deducted once=True ({stock_before}->{stock_after})",
        )
    return ScenarioResult(
        name="order_idempotency",
        passed=False,
        detail=f"same order={same_order}, stock deducted once={deducted_once}",
    )


def run_all() -> Report:
    """Run every fault scenario and aggregate the outcome."""
    engine = build_engine()
    inject_worker_crash(engine)

    client = TestClient(create_simulator_app())
    scenarios: list[ScenarioResult] = [scenario_worker_recovery(engine)]

    client.put("/scenario/timeout")
    scenarios.append(scenario_erp_timeout_retry(client))

    client.put("/scenario/happy_path")
    scenarios.append(scenario_order_idempotency(client))

    failures = [s for s in scenarios if not s["passed"]]
    return Report(
        total=len(scenarios),
        passed=len(scenarios) - len(failures),
        scenarios=scenarios,
        failures=failures,
    )


def main() -> int:
    """Print the report; exit non-zero when any scenario fails."""
    report = run_all()
    print(f"fault-injection report: {report['passed']}/{report['total']} passed")
    for scenario in report["scenarios"]:
        mark = "PASS" if scenario["passed"] else "FAIL"
        print(f"  [{mark}] {scenario['name']}: {scenario['detail']}")
    return 0 if report["failures"] == [] else 1


if __name__ == "__main__":
    raise SystemExit(main())
