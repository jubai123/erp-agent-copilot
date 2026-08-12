"""Unit tests for the worker's ERP-simulator executor write tools.

The worker's async executor (apps/worker/executor.py) resolves the tool calls
inside execute_ready_steps. The write-path enablement adds two tools:
createOrder actually creates an order in the simulator's in-memory store and
deducts stock, replaying the existing order for the same idempotency key
(at-most-once at the tool layer); getOrderByOrderId reads one back. Scenario
handling (timeout / stock_insufficient) mirrors the simulator HTTP routes so
the e2e scenario tests keep passing.

The executor is async; tests drive it with asyncio.run (the suite has no
asyncio-mode config, so plain sync tests stay portable).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from apps.erp_simulator.data.products import PRODUCT_BY_NAME
from apps.worker.executor import erp_simulator_executor
from erp_copilot.tools.tool_result import ToolResult


def _run(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    return asyncio.run(erp_simulator_executor(tool_name, arguments))


def _stock_of(name: str) -> int:
    return PRODUCT_BY_NAME[name].quantity_in_stock


def _create_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "product_id": 1,
        "supplier_id": 3,
        "quantity": 20,
        "region": "上海",
        "idempotency_key": f"executor-test-{uuid.uuid4().hex}",
    }
    args.update(overrides)
    return args


class TestCreateOrder:
    def test_create_order_success_deducts_stock(self) -> None:
        before = _stock_of("苹果")
        result = _run("createOrder", _create_args())

        assert result.status == "SUCCEEDED"
        data = result.data
        assert data is not None
        assert data["product_id"] == 1
        assert data["supplier_id"] == 3
        assert data["quantity"] == 20
        assert data["region"] == "上海"
        assert data["status"] == "CREATED"
        assert data["order_id"]
        assert _stock_of("苹果") == before - 20

    def test_create_order_idempotent_replay_deducts_once(self) -> None:
        args = _create_args()
        before = _stock_of("苹果")

        first = _run("createOrder", args)
        second = _run("createOrder", args)

        assert first.status == "SUCCEEDED"
        assert second.status == "SUCCEEDED"
        assert second.data is not None and first.data is not None
        # Same key -> same order replayed, stock deducted only once.
        assert second.data["order_id"] == first.data["order_id"]
        assert _stock_of("苹果") == before - first.data["quantity"]

    def test_create_order_insufficient_stock(self) -> None:
        before = _stock_of("苹果")
        result = _run("createOrder", _create_args(quantity=before + 1))

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INSUFFICIENT_STOCK"
        assert result.error.is_retryable is False
        assert _stock_of("苹果") == before

    def test_create_order_stock_insufficient_scenario(self, monkeypatch) -> None:
        import apps.erp_simulator.scenarios as _scenarios

        monkeypatch.setattr(_scenarios, "_current_scenario", "stock_insufficient")
        before = _stock_of("苹果")
        result = _run("createOrder", _create_args(quantity=1))

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INSUFFICIENT_STOCK"
        assert _stock_of("苹果") == before

    def test_create_order_product_not_found(self) -> None:
        result = _run("createOrder", _create_args(product_id=999))

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_create_order_requires_idempotency_key(self) -> None:
        args = _create_args()
        del args["idempotency_key"]
        result = _run("createOrder", args)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"

    def test_create_order_invalid_quantity(self) -> None:
        result = _run("createOrder", _create_args(quantity=0))

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class TestGetOrderByOrderId:
    def test_get_order_by_id_returns_created_order(self) -> None:
        created = _run("createOrder", _create_args())
        assert created.data is not None

        result = _run("getOrderByOrderId", {"order_id": created.data["order_id"]})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["order_id"] == created.data["order_id"]
        assert result.data["status"] == "CREATED"

    def test_get_order_by_id_not_found(self) -> None:
        result = _run("getOrderByOrderId", {"order_id": "no-such-order"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_NOT_FOUND"


class TestErrorMapping:
    def test_timeout_scenario_fails_create_order_retryable(self, monkeypatch) -> None:
        import apps.erp_simulator.scenarios as _scenarios

        monkeypatch.setattr(_scenarios, "_current_scenario", "timeout")
        result = _run("createOrder", _create_args())

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "TIMEOUT"
        assert result.error.is_retryable is True

    def test_unknown_tool_fails_closed(self) -> None:
        result = _run("updateOrderStatus", {"order_id": "x", "status": "SHIPPED"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UNKNOWN_TOOL"
