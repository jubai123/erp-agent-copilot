"""Unit tests for the terminal write-path reconciliation service — task 7.11.

The service (application/reconciliation.py) verifies that the ERP's actual
final state matches the plan's write intent for every COMPLETED write step and
records the verdict on erp_reconciliation_success_total (outcome=
consistent/mismatch). It is the online sentinel for write-path correctness —
"AI 说做了，系统真的做了吗" — and upgrades the crash-recovery error code
RECOVERY_RECONCILIATION_REQUIRED (docs/03 §9) from an error into a production
metric.

Read-back of the ERP state is injected (``read_order``), so every test drives a
stub and no network is involved; the real async executor is adapted by
``build_erp_read_order``. One test drives the real in-process simulator
executor end-to-end so the acceptance "审批 → 幂等创建 → 对账确认一致" has a
concrete proof, not just stubbed logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from erp_copilot.agent.state import AgentState, AgentStatus, Plan, PlanStep, StepResult
from erp_copilot.application.reconciliation import (
    ReconciliationQueryError,
    StepReconciliation,
    build_erp_read_order,
    build_terminal_reconciler,
    reconcile_write_paths,
    verify_write_step,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.observability.metrics import create_metrics
from erp_copilot.tools.tool_result import ToolResult


def _order_result(order_id: str, **overrides: Any) -> ToolResult:
    data = {
        "order_id": order_id,
        "product_id": 10094,
        "quantity": 1,
        "supplier_id": 2,
        "region": "上海",
        "status": "CREATED",
    }
    data.update(overrides)
    return ToolResult.success(tool_version_id="createOrder", data=data)


def _order_executor(order: dict[str, Any]) -> Callable[..., Awaitable[ToolResult]]:
    """Stub async executor that serves one order on getOrderByOrderId."""

    async def executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if tool_name != "getOrderByOrderId":
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="UNKNOWN_TOOL",
                error_message=f"Unknown tool {tool_name!r}",
            )
        order_id = arguments.get("order_id")
        if order_id == order.get("order_id"):
            return ToolResult.success(tool_version_id=tool_name, data=dict(order))
        return ToolResult.failure(
            tool_version_id=tool_name,
            error_code="ORDER_NOT_FOUND",
            error_message=f"Order '{order_id}' not found",
        )

    return executor


def _chained_write_state(order: dict[str, Any]) -> AgentState:
    """A completed create chain: getProductByName → querySuppliers → createOrder.

    The createOrder arguments carry the cross-step source markers (step:s1 /
    step:s2) exactly as the planner emits them, so the reconciler must resolve
    them against the earlier steps' results to learn the true intent.
    """
    return AgentState(
        run_id="r1",
        tenant_id="t1",
        query="帮我在上海下一单 1 KG 苹果",
        status=AgentStatus.SUCCEEDED,
        plan=Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="getProductByName",
                    risk_level=ToolRiskLevel.READ,
                    arguments={"name": "苹果"},
                ),
                PlanStep(
                    step_id="s2",
                    tool_name="querySuppliersByDeliveryRegion",
                    risk_level=ToolRiskLevel.READ,
                    arguments={"region": "上海"},
                ),
                PlanStep(
                    step_id="s3",
                    tool_name="createOrder",
                    risk_level=ToolRiskLevel.WRITE,
                    idempotency_key="k3",
                    arguments={
                        "product_id": "",
                        "supplier_id": "",
                        "quantity": 1,
                        "region": "上海",
                    },
                    argument_sources={"product_id": "step:s1", "supplier_id": "step:s2"},
                    depends_on=["s1", "s2"],
                    fallback="取消新建订单以补偿",
                ),
            ]
        ),
        step_results={
            "s1": StepResult(
                step_id="s1",
                status=StepStatus.COMPLETED,
                data={"product_id": 10094, "name": "苹果"},
            ),
            "s2": StepResult(
                step_id="s2",
                status=StepStatus.COMPLETED,
                data={"supplier_id": 2},
            ),
            "s3": StepResult(
                step_id="s3",
                status=StepStatus.COMPLETED,
                data=dict(order),
            ),
        },
    )


class TestVerifyWriteStep:
    """The pure decision: does the ERP state satisfy the write intent?"""

    def test_create_order_consistent(self) -> None:
        actual = {"product_id": 10094, "quantity": 1, "supplier_id": 2, "region": "上海"}
        verdict = verify_write_step(
            "createOrder",
            {"product_id": 10094, "quantity": 1, "supplier_id": 2, "region": "上海"},
            actual,
        )
        assert verdict == "consistent"

    def test_create_order_mismatch_on_any_intent_field(self) -> None:
        args = {"product_id": 10094, "quantity": 1, "supplier_id": 2, "region": "上海"}
        # Quantity silently altered by the ERP side — the classic drift signal.
        actual = {"product_id": 10094, "quantity": 3, "supplier_id": 2, "region": "上海"}
        assert verify_write_step("createOrder", args, actual) == "mismatch"

    def test_create_order_missing_order_is_mismatch(self) -> None:
        # The write returned an order id, but the ERP holds no such order — the
        # write provably did not take effect.
        args = {"product_id": 10094, "quantity": 1, "supplier_id": 2, "region": "上海"}
        assert verify_write_step("createOrder", args, None) == "mismatch"

    def test_update_order_status_consistent_and_mismatch(self) -> None:
        args = {"order_id": "o1", "status": "SHIPPED"}
        assert verify_write_step("updateOrderStatus", args, {"status": "SHIPPED"}) == "consistent"
        assert verify_write_step("updateOrderStatus", args, {"status": "CREATED"}) == "mismatch"
        assert verify_write_step("updateOrderStatus", args, None) == "mismatch"

    def test_cancel_order_consistent_when_cancelled(self) -> None:
        args = {"order_id": "o1"}
        assert verify_write_step("cancelOrder", args, {"status": "CANCELLED"}) == "consistent"
        assert verify_write_step("cancelOrder", args, {"status": "CREATED"}) == "mismatch"

    def test_non_verifiable_tool_returns_none(self) -> None:
        assert verify_write_step("getProductByName", {"name": "苹果"}, {}) is None


class TestReconcileWritePaths:
    """Which steps get a verdict, and how the read-back is driven."""

    def test_only_completed_write_steps_are_reconciled(self) -> None:
        state = _chained_write_state({"order_id": "o1", "quantity": 1})
        state.step_results["s3"] = StepResult(
            step_id="s3", status=StepStatus.FAILED, error_code="TIMEOUT"
        )
        assert reconcile_write_paths(state, lambda _: {}) == []

    def test_resolves_chained_arguments_before_comparing(self) -> None:
        # s3's arguments say product_id/supplier_id come from step:s1 / step:s2;
        # the reconciler must resolve them to 10094/2 before comparing against
        # the ERP order, or every chained DAG would fabricate a mismatch.
        order = {"order_id": "o1", "product_id": 10094, "quantity": 1, "supplier_id": 2,
                 "region": "上海", "status": "CREATED"}
        verdicts = reconcile_write_paths(_chained_write_state(order), lambda _: dict(order))
        assert [v.step_id for v in verdicts] == ["s3"]
        assert verdicts[0].verdict == "consistent"

    def test_mismatch_when_erp_state_differs_from_intent(self) -> None:
        order = {"order_id": "o1", "product_id": 10094, "quantity": 9, "supplier_id": 2,
                 "region": "上海", "status": "CREATED"}
        verdicts = reconcile_write_paths(_chained_write_state(order), lambda _: dict(order))
        assert verdicts == [
            StepReconciliation(step_id="s3", tool_name="createOrder", verdict="mismatch",
                               detail="createOrder 字段 quantity 不一致 (意图 1 vs ERP 9)")
        ]

    def test_missing_order_in_erp_is_mismatch(self) -> None:
        # The write returned an order id, but the ERP holds no such order — the
        # claimed effect provably did not land.
        state = _chained_write_state(
            {"order_id": "o1", "product_id": 10094, "quantity": 1,
             "supplier_id": 2, "region": "上海", "status": "CREATED"}
        )
        verdicts = reconcile_write_paths(state, lambda _: None)
        assert [v.verdict for v in verdicts] == ["mismatch"]

    def test_read_back_exception_skips_step_not_crash(self) -> None:
        def read_order(_order_id: str) -> dict[str, Any]:
            raise ReconciliationQueryError("ERP unreachable")

        assert reconcile_write_paths(_chained_write_state({}), read_order) == []

    def test_plan_without_write_steps_yields_no_verdicts(self) -> None:
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="苹果多少钱",
            status=AgentStatus.SUCCEEDED,
            plan=Plan(
                steps=[
                    PlanStep(
                        step_id="s1",
                        tool_name="getProductByName",
                        risk_level=ToolRiskLevel.READ,
                        arguments={"name": "苹果"},
                    )
                ]
            ),
        )
        assert reconcile_write_paths(state, lambda _: {}) == []


class TestReadOrderAdapter:
    """The async executor → sync read_order bridge."""

    def test_returns_order_dict_on_success(self) -> None:
        order = {"order_id": "o1", "status": "CREATED"}
        read_order = build_erp_read_order(_order_executor(order))
        assert read_order("o1") == order

    def test_returns_none_on_order_not_found(self) -> None:
        read_order = build_erp_read_order(_order_executor({"order_id": "o1", "status": "CREATED"}))
        assert read_order("missing") is None

    def test_transport_failure_raises_not_none(self) -> None:
        # A failed query proves nothing about the order's existence, so it must
        # raise (the caller skips the step) rather than masquerade as "missing"
        # (which would fabricate a mismatch).
        async def executor(tool_name: str, _args: dict[str, Any]) -> ToolResult:
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="TIMEOUT",
                error_message="erp timeout",
                is_retryable=True,
            )

        read_order = build_erp_read_order(executor)
        with pytest.raises(ReconciliationQueryError):
            read_order("o1")


class TestTerminalReconciler:
    """The persist_run-facing wiring: verdicts in, metric out."""

    def test_records_consistent_verdict_on_metric(self) -> None:
        metrics = create_metrics()
        order = {"order_id": "o1", "product_id": 10094, "quantity": 1, "supplier_id": 2,
                 "region": "上海", "status": "CREATED"}
        reconciler = build_terminal_reconciler(_order_executor(order), metrics=metrics)
        verdicts = reconciler(_chained_write_state(order))
        assert [v.verdict for v in verdicts] == ["consistent"]
        assert (
            metrics.reconciliation_success.labels(outcome="consistent")._value.get() == 1.0
        )
        assert (
            metrics.reconciliation_success.labels(outcome="mismatch")._value.get() == 0.0
        )
