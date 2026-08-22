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

from apps.erp_simulator.data.products import PRODUCT_BY_ID, PRODUCT_BY_NAME
from apps.erp_simulator.data.suppliers import SUPPLIER_BY_ID, SUPPLIER_BY_NAME
from apps.worker.executor import _with_circuit_breaker, _with_rate_limit, erp_simulator_executor
from erp_copilot.agent.circuit_breaker import CIRCUIT_OPEN_CODE, RedisCircuitBreaker
from erp_copilot.agent.rate_limiter import RATE_LIMITED_CODE, RedisRateLimiter
from erp_copilot.tools.tool_result import ToolResult


def _run(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    return asyncio.run(erp_simulator_executor(tool_name, arguments))


def _stock_of(name: str) -> int:
    return PRODUCT_BY_NAME[name].quantity_in_stock


def _m3_key() -> str:
    return f"m3-executor-{uuid.uuid4().hex}"


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


class TestOrderQueryTools:
    """Set-query order tools return {"orders": [...]}; an empty list is a legal
    business answer, so these tools never fail on no-match (unlike the single
    lookups getOrderByOrderId / getSupplierByName)."""

    def _create_order(self) -> dict[str, Any]:
        created = _run("createOrder", _create_args())
        assert created.status == "SUCCEEDED"
        assert created.data is not None
        return created.data

    def test_get_orders_by_supplier_includes_created_order(self) -> None:
        order = self._create_order()

        result = _run("getOrdersBySupplierId", {"supplier_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert order["order_id"] in [o["order_id"] for o in result.data["orders"]]

    def test_get_orders_by_supplier_no_match_is_empty_success(self) -> None:
        result = _run("getOrdersBySupplierId", {"supplier_id": 999})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_orders_by_supplier_missing_arg(self) -> None:
        result = _run("getOrdersBySupplierId", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_get_orders_by_product_includes_created_order(self) -> None:
        order = self._create_order()

        result = _run("getByProductId", {"product_id": 1})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert order["order_id"] in [o["order_id"] for o in result.data["orders"]]

    def test_get_orders_by_product_no_match_is_empty_success(self) -> None:
        result = _run("getByProductId", {"product_id": 999})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_orders_by_product_missing_arg(self) -> None:
        result = _run("getByProductId", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_get_orders_by_status_includes_created_order(self) -> None:
        order = self._create_order()

        result = _run("getByOrderStatus", {"status": "CREATED"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert order["order_id"] in [o["order_id"] for o in result.data["orders"]]

    def test_get_orders_by_status_no_match_is_empty_success(self) -> None:
        result = _run("getByOrderStatus", {"status": "DELIVERED"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_orders_by_status_missing_arg(self) -> None:
        result = _run("getByOrderStatus", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_get_orders_by_time_range_includes_created_order(self) -> None:
        order = self._create_order()

        result = _run("getByTimeRange", {"start_date": "2000-01-01", "end_date": "2100-01-01"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert order["order_id"] in [o["order_id"] for o in result.data["orders"]]

    def test_get_orders_by_time_range_no_match_is_empty_success(self) -> None:
        result = _run("getByTimeRange", {"start_date": "2099-01-01", "end_date": "2099-12-31"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_orders_by_time_range_missing_arg(self) -> None:
        result = _run("getByTimeRange", {"start_date": "2024-01-01"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class TestUpdateOrderStatus:
    """updateOrderStatus enforces the rules.yaml order state machine."""

    def _created_order_id(self) -> str:
        created = _run("createOrder", _create_args())
        assert created.status == "SUCCEEDED"
        assert created.data is not None
        return created.data["order_id"]

    def test_legal_transition_applies(self) -> None:
        order_id = self._created_order_id()

        result = _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "CONFIRMED", "idempotency_key": "u1"},
        )

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["order_id"] == order_id
        assert result.data["status"] == "CONFIRMED"

    def test_replay_same_key_returns_without_reapplying(self) -> None:
        order_id = self._created_order_id()
        first = _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "CONFIRMED", "idempotency_key": "u-replay"},
        )
        assert first.status == "SUCCEEDED"

        replayed = _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "SHIPPED", "idempotency_key": "u-replay"},
        )

        assert replayed.status == "SUCCEEDED"
        assert replayed.data is not None
        assert replayed.data["status"] == "CONFIRMED"  # recorded result, not re-applied

    def test_illegal_transition_fails(self) -> None:
        order_id = self._created_order_id()

        result = _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "DELIVERED", "idempotency_key": "u-skip"},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_STATUS_TRANSITION"
        assert result.error.is_retryable is False

    def test_terminal_order_rejects_update(self) -> None:
        order_id = self._created_order_id()
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "CONFIRMED", "idempotency_key": "u-a"},
        )
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "SHIPPED", "idempotency_key": "u-b"},
        )
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "DELIVERED", "idempotency_key": "u-c"},
        )

        result = _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "CANCELLED", "idempotency_key": "u-d"},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_STATUS_TRANSITION"

    def test_unknown_order_is_not_found(self) -> None:
        result = _run(
            "updateOrderStatus",
            {"order_id": "no-such", "status": "CONFIRMED", "idempotency_key": "u-none"},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_NOT_FOUND"

    def test_missing_args_are_invalid(self) -> None:
        missing_order = _run("updateOrderStatus", {"status": "CONFIRMED", "idempotency_key": "k"})
        missing_status = _run("updateOrderStatus", {"order_id": "x", "idempotency_key": "k"})
        missing_key = _run("updateOrderStatus", {"order_id": "x", "status": "CONFIRMED"})

        assert missing_order.error is not None
        assert missing_order.error.error_code == "INVALID_ARGUMENT"
        assert missing_status.error is not None
        assert missing_status.error.error_code == "INVALID_ARGUMENT"
        assert missing_key.error is not None
        assert missing_key.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"


class TestCancelOrder:
    """cancelOrder cancels CREATED/CONFIRMED/SHIPPED orders (at-most-once)."""

    def _created_order_id(self) -> str:
        created = _run("createOrder", _create_args())
        assert created.status == "SUCCEEDED"
        assert created.data is not None
        return created.data["order_id"]

    def test_cancels_created_order(self) -> None:
        order_id = self._created_order_id()

        result = _run("cancelOrder", {"order_id": order_id, "idempotency_key": "c1"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["order_id"] == order_id
        assert result.data["status"] == "CANCELLED"

    def test_cancels_shipped_order(self) -> None:
        order_id = self._created_order_id()
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "CONFIRMED", "idempotency_key": "c-a"},
        )
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "SHIPPED", "idempotency_key": "c-b"},
        )

        result = _run("cancelOrder", {"order_id": order_id, "idempotency_key": "c-shipped"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["status"] == "CANCELLED"

    def test_delivered_order_cannot_cancel(self) -> None:
        order_id = self._created_order_id()
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "CONFIRMED", "idempotency_key": "c-d-a"},
        )
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "SHIPPED", "idempotency_key": "c-d-b"},
        )
        _run(
            "updateOrderStatus",
            {"order_id": order_id, "status": "DELIVERED", "idempotency_key": "c-d-c"},
        )

        result = _run("cancelOrder", {"order_id": order_id, "idempotency_key": "c-d-delivered"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_STATUS_TRANSITION"

    def test_cancelled_order_cannot_cancel_twice(self) -> None:
        order_id = self._created_order_id()
        first = _run("cancelOrder", {"order_id": order_id, "idempotency_key": "c-1"})
        assert first.status == "SUCCEEDED"

        second = _run("cancelOrder", {"order_id": order_id, "idempotency_key": "c-2"})

        assert second.status == "FAILED"
        assert second.error is not None
        assert second.error.error_code == "INVALID_STATUS_TRANSITION"

    def test_unknown_order_is_not_found(self) -> None:
        result = _run("cancelOrder", {"order_id": "no-such", "idempotency_key": "c-none"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_NOT_FOUND"

    def test_missing_idempotency_key_is_rejected(self) -> None:
        result = _run("cancelOrder", {"order_id": "x"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"


class TestSupplierRead:
    def test_get_supplier_by_status_carries_canonical_supplier_id(self) -> None:
        result = _run("getSupplierByStatus", {"status": "AVAILABLE"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        # First (deterministic) available supplier — the id the create DAG's
        # argument_sources={"supplier_id": "step:s2"} resolves from.
        assert result.data["supplier_id"] == 3
        assert {s["supplier_id"] for s in result.data["suppliers"]} == {3, 4, 6, 7}

    def test_query_suppliers_by_region_returns_region_matches(self) -> None:
        result = _run("querySuppliersByDeliveryRegion", {"region": "上海"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["supplier_id"] == 3
        assert [s["name"] for s in result.data["suppliers"]] == ["华东物流"]

    def test_query_suppliers_by_unknown_region_returns_empty(self) -> None:
        result = _run("querySuppliersByDeliveryRegion", {"region": "不存在区域"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"suppliers": []}


class TestGetSupplierByName:
    def test_lookup_by_name_returns_flat_supplier(self) -> None:
        result = _run("getSupplierByName", {"name": "华东物流"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        # A single-supplier lookup returns a flat dict (mirrors getProductById),
        # not the {"suppliers": [...]} envelope the set-query tools use.
        assert result.data["supplier_id"] == 3
        assert result.data["name"] == "华东物流"
        assert result.data["regions"] == ["上海", "南京"]
        assert result.data["status"] == "AVAILABLE"

    def test_unknown_name_is_supplier_not_found(self) -> None:
        result = _run("getSupplierByName", {"name": "不存在供应商"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_NOT_FOUND"
        assert result.error.is_retryable is False

    def test_missing_name_is_invalid_argument(self) -> None:
        result = _run("getSupplierByName", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class TestGetSupplierById:
    def test_lookup_by_id_returns_flat_supplier(self) -> None:
        result = _run("getSupplierById", {"supplier_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["supplier_id"] == 3
        assert result.data["name"] == "华东物流"
        assert result.data["regions"] == ["上海", "南京"]
        assert result.data["status"] == "AVAILABLE"

    def test_unknown_id_is_supplier_not_found(self) -> None:
        result = _run("getSupplierById", {"supplier_id": 999})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_NOT_FOUND"
        assert result.error.is_retryable is False

    def test_missing_id_is_invalid_argument(self) -> None:
        result = _run("getSupplierById", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


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
        result = _run("noSuchTool", {"order_id": "x"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UNKNOWN_TOOL"


class TestRequiredArgumentValidation:
    """Tools fail closed (INVALID_ARGUMENT) when a required argument is missing.

    Arguments come from planner-generated tool calls (LLM output, untrusted), so
    a missing key must be rejected instead of being passed down as None into the
    simulator data layer.
    """

    def test_get_product_missing_name(self) -> None:
        result = _run("getProductByName", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_create_order_missing_product_id(self) -> None:
        args = _create_args()
        del args["product_id"]
        result = _run("createOrder", args)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_create_order_missing_supplier_id(self) -> None:
        args = _create_args()
        del args["supplier_id"]
        result = _run("createOrder", args)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_create_order_missing_region(self) -> None:
        args = _create_args()
        del args["region"]
        result = _run("createOrder", args)

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_get_order_missing_order_id(self) -> None:
        result = _run("getOrderByOrderId", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class TestGetProductById:
    def test_get_product_by_id_returns_product(self) -> None:
        result = _run("getProductById", {"product_id": 1})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["product_id"] == 1
        assert result.data["name"] == "苹果"
        assert result.data["price"] == 10.0

    def test_get_product_by_id_not_found(self) -> None:
        result = _run("getProductById", {"product_id": 999})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_get_product_by_id_missing_arg(self) -> None:
        result = _run("getProductById", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"

    def test_get_product_by_id_non_int(self) -> None:
        result = _run("getProductById", {"product_id": "1"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class TestGetProductSubstitutes:
    def test_by_id_returns_seed_substitute(self) -> None:
        result = _run("getProductSubstitutes", {"product_id": 1})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert [p["name"] for p in result.data["substitutes"]] == ["香蕉"]

    def test_by_name_returns_seed_substitute(self) -> None:
        result = _run("getProductSubstitutesByName", {"name": "苹果"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert [p["name"] for p in result.data["substitutes"]] == ["香蕉"]

    def test_no_substitute_returns_empty_list(self) -> None:
        result = _run("getProductSubstitutes", {"product_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data == {"substitutes": []}

    def test_by_id_not_found(self) -> None:
        result = _run("getProductSubstitutes", {"product_id": 999})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_by_name_not_found(self) -> None:
        result = _run("getProductSubstitutesByName", {"name": "榴莲"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_missing_arguments_fail_closed(self) -> None:
        by_id = _run("getProductSubstitutes", {})
        by_name = _run("getProductSubstitutesByName", {})

        assert by_id.error is not None and by_id.error.error_code == "INVALID_ARGUMENT"
        assert by_name.error is not None and by_name.error.error_code == "INVALID_ARGUMENT"


class TestGetBatchProductByProductIds:
    def test_batch_returns_id_range_in_order(self) -> None:
        result = _run("getBatchProductByProductIds", {"start_id": 1, "end_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert [p["product_id"] for p in result.data["products"]] == [1, 2, 3]

    def test_batch_empty_range_is_success(self) -> None:
        result = _run("getBatchProductByProductIds", {"start_id": 100, "end_id": 200})

        assert result.status == "SUCCEEDED"
        assert result.data == {"products": []}

    def test_batch_reversed_range_returns_empty(self) -> None:
        result = _run("getBatchProductByProductIds", {"start_id": 5, "end_id": 1})

        assert result.status == "SUCCEEDED"
        assert result.data == {"products": []}

    def test_batch_missing_args(self) -> None:
        result = _run("getBatchProductByProductIds", {})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


def _unique_product_name() -> str:
    return f"测试商品-{uuid.uuid4().hex[:8]}"


class TestAddProduct:
    def test_add_creates_indexed_product(self) -> None:
        name = _unique_product_name()
        result = _run(
            "addProduct",
            {"name": name, "price": 9.9, "quantity_in_stock": 5, "idempotency_key": _m3_key()},
        )

        assert result.status == "SUCCEEDED"
        data = result.data
        assert data is not None
        assert data["name"] == name
        assert data["price"] == 9.9
        assert data["stock"] == 5
        assert PRODUCT_BY_NAME[name].name == name

    def test_add_idempotent_replay_returns_same_product(self) -> None:
        name = _unique_product_name()
        args = {"name": name, "price": 9.9, "quantity_in_stock": 5, "idempotency_key": _m3_key()}

        first = _run("addProduct", args)
        second = _run("addProduct", args)

        assert first.status == "SUCCEEDED"
        assert second.status == "SUCCEEDED"
        assert second.data is not None and first.data is not None
        assert second.data["product_id"] == first.data["product_id"]

    def test_duplicate_name_is_business_failure(self) -> None:
        name = _unique_product_name()
        _run(
            "addProduct",
            {"name": name, "price": 1.0, "quantity_in_stock": 1, "idempotency_key": _m3_key()},
        )

        result = _run(
            "addProduct",
            {"name": name, "price": 2.0, "quantity_in_stock": 2, "idempotency_key": _m3_key()},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_ALREADY_EXISTS"

    def test_requires_idempotency_key(self) -> None:
        result = _run("addProduct", {"name": "x", "price": 1.0, "quantity_in_stock": 1})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"

    def test_invalid_price_is_invalid_argument(self) -> None:
        result = _run(
            "addProduct",
            {"name": "x", "price": "高", "quantity_in_stock": 1, "idempotency_key": _m3_key()},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class TestUpdateProductDescription:
    def test_updates_description_in_place(self) -> None:
        result = _run(
            "updateProductDescription",
            {"product_id": 1, "description": "新描述", "idempotency_key": _m3_key()},
        )

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        assert PRODUCT_BY_ID[1].description == "新描述"

    def test_idempotent_replay_returns_recorded_result(self) -> None:
        key = _m3_key()
        first = _run(
            "updateProductDescription",
            {"product_id": 1, "description": "A", "idempotency_key": key},
        )
        replayed = _run(
            "updateProductDescription",
            {"product_id": 1, "description": "B", "idempotency_key": key},
        )

        assert first.status == "SUCCEEDED"
        assert replayed.status == "SUCCEEDED"
        assert PRODUCT_BY_ID[1].description == "A"  # recorded result, not re-applied

    def test_unknown_product_is_not_found(self) -> None:
        result = _run(
            "updateProductDescription",
            {"product_id": 999999, "description": "x", "idempotency_key": _m3_key()},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_requires_idempotency_key(self) -> None:
        result = _run("updateProductDescription", {"product_id": 1, "description": "x"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"


class TestUpdateProductSubstitutes:
    def test_sets_substitute_by_name(self) -> None:
        result = _run(
            "updateProductSubstitutes",
            {"product_id": 1, "substitute_name": "香蕉", "idempotency_key": _m3_key()},
        )

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        assert PRODUCT_BY_ID[1].substitute_product_id == 2

    def test_unknown_substitute_is_business_failure(self) -> None:
        result = _run(
            "updateProductSubstitutes",
            {"product_id": 1, "substitute_name": "不存在的替代品", "idempotency_key": _m3_key()},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUBSTITUTE_NOT_FOUND"

    def test_unknown_product_is_not_found(self) -> None:
        result = _run(
            "updateProductSubstitutes",
            {"product_id": 999999, "substitute_name": "香蕉", "idempotency_key": _m3_key()},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_requires_idempotency_key(self) -> None:
        result = _run("updateProductSubstitutes", {"product_id": 1, "substitute_name": "香蕉"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"


class TestRemoveProduct:
    def test_remove_by_name_clears_indexes(self) -> None:
        name = _unique_product_name()
        created = _run(
            "addProduct",
            {"name": name, "price": 1.0, "quantity_in_stock": 1, "idempotency_key": _m3_key()},
        )
        product_id = created.data["product_id"]

        result = _run("removeProductByName", {"name": name, "idempotency_key": _m3_key()})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["product_id"] == product_id
        assert PRODUCT_BY_NAME.get(name) is None
        assert PRODUCT_BY_ID.get(product_id) is None

    def test_remove_by_id_clears_indexes(self) -> None:
        name = _unique_product_name()
        created = _run(
            "addProduct",
            {"name": name, "price": 1.0, "quantity_in_stock": 1, "idempotency_key": _m3_key()},
        )
        product_id = created.data["product_id"]

        result = _run("removeProductById", {"product_id": product_id, "idempotency_key": _m3_key()})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["product_id"] == product_id
        assert PRODUCT_BY_NAME.get(name) is None

    def test_remove_unknown_is_not_found(self) -> None:
        result = _run("removeProductByName", {"name": "不存在的商品", "idempotency_key": _m3_key()})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"

    def test_requires_idempotency_key(self) -> None:
        result = _run("removeProductById", {"product_id": 1})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"


def _unique_supplier_name() -> str:
    return f"测试物流-{uuid.uuid4().hex[:8]}"


class TestAddSupplier:
    def test_add_creates_indexed_supplier(self) -> None:
        name = _unique_supplier_name()
        result = _run(
            "addSuppliers",
            {
                "name": name,
                "regions": ["上海"],
                "status": "AVAILABLE",
                "idempotency_key": _m3_key(),
            },
        )

        assert result.status == "SUCCEEDED"
        data = result.data
        assert data is not None
        assert data["name"] == name
        assert data["regions"] == ["上海"]
        assert SUPPLIER_BY_NAME[name].name == name

    def test_idempotent_replay_returns_same_supplier(self) -> None:
        name = _unique_supplier_name()
        args = {
            "name": name,
            "regions": ["上海"],
            "status": "AVAILABLE",
            "idempotency_key": _m3_key(),
        }

        first = _run("addSuppliers", args)
        second = _run("addSuppliers", args)

        assert first.status == "SUCCEEDED"
        assert second.status == "SUCCEEDED"
        assert second.data is not None and first.data is not None
        assert second.data["supplier_id"] == first.data["supplier_id"]

    def test_duplicate_name_is_business_failure(self) -> None:
        name = _unique_supplier_name()
        _run(
            "addSuppliers",
            {
                "name": name,
                "regions": ["上海"],
                "status": "AVAILABLE",
                "idempotency_key": _m3_key(),
            },
        )

        result = _run(
            "addSuppliers",
            {
                "name": name,
                "regions": ["北京"],
                "status": "AVAILABLE",
                "idempotency_key": _m3_key(),
            },
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_ALREADY_EXISTS"

    def test_requires_idempotency_key(self) -> None:
        result = _run("addSuppliers", {"name": "x", "regions": ["上海"], "status": "AVAILABLE"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"

    def test_invalid_regions_is_invalid_argument(self) -> None:
        result = _run(
            "addSuppliers",
            {"name": "x", "regions": "上海", "status": "AVAILABLE", "idempotency_key": _m3_key()},
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_ARGUMENT"


class _FakeRedis:
    """Minimal redis-py stand-in for the breaker / rate-limiter wrapper tests.

    Models the commands the breaker (exists/set/incr/expire/delete) and the
    rate limiter (zremrangebyscore/zcard/zadd) touch; ``deleted`` records every
    key cleared so a test can assert the success path reset the window.
    """

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.deleted: list[str] = []
        self.zsets: dict[str, dict[str, float]] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.data else 0

    def set(self, key: str, value: object, ex: object = None) -> bool:
        self.data[key] = str(value)
        return True

    def incr(self, key: str) -> int:
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    def expire(self, key: str, seconds: int) -> bool:
        return True

    def delete(self, key: str) -> int:
        if key in self.data:
            del self.data[key]
            self.deleted.append(key)
            return 1
        return 0

    def zremrangebyscore(self, key: str, min_: float, max_: float) -> int:
        z = self.zsets.setdefault(key, {})
        stale = [member for member, score in z.items() if min_ <= score <= max_]
        for member in stale:
            del z[member]
        return len(stale)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        z = self.zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in z)
        z.update(mapping)
        return added


class TestCircuitBreakerWrapper:
    """_with_circuit_breaker fails fast when the breaker is OPEN and records the
    outcome otherwise — the breaker sits inside the retry executor, so an OPEN
    circuit returns a non-retryable CIRCUIT_OPEN that stops retries immediately."""

    def test_open_circuit_short_circuits_without_calling_executor(self) -> None:
        redis = _FakeRedis()
        redis.set("erp:cb:mcp:test:open", "1")
        breaker = RedisCircuitBreaker(redis, "mcp:test", failure_threshold=5)

        calls: list[str] = []

        async def spy(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id=tool_name, data={})

        guarded = _with_circuit_breaker(spy, breaker)
        result = asyncio.run(guarded("getProductByName", {"name": "苹果"}))

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == CIRCUIT_OPEN_CODE
        assert result.error.is_retryable is False
        assert calls == []  # upstream never touched

    def test_closed_circuit_executes_and_records_outcome(self) -> None:
        redis = _FakeRedis()
        breaker = RedisCircuitBreaker(redis, "mcp:test", failure_threshold=5)
        # Seed a failure window so the success path has something to clear.
        redis.incr("erp:cb:mcp:test:failures")

        calls: list[str] = []

        async def spy(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id=tool_name, data={"ok": True})

        guarded = _with_circuit_breaker(spy, breaker)
        result = asyncio.run(guarded("getProductByName", {"name": "苹果"}))

        assert result.status == "SUCCEEDED"
        assert result.data == {"ok": True}
        assert calls == ["getProductByName"]
        # A success clears the seeded failure window.
        assert "erp:cb:mcp:test:failures" not in redis.data


class _FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class TestRateLimitWrapper:
    """_with_rate_limit gates calls against the sliding-window budget and returns
    a non-retryable RATE_LIMITED before the upstream (or the circuit breaker) is
    ever touched — the limiter sits outside the breaker so self-throttling never
    trips the shared circuit."""

    def test_over_limit_short_circuits_without_calling_executor(self) -> None:
        redis = _FakeRedis()
        clock = _FakeClock()
        limiter = RedisRateLimiter(redis, "mcp:test", limit=2, now=clock)

        calls: list[str] = []

        async def spy(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id=tool_name, data={})

        guarded = _with_rate_limit(
            _with_circuit_breaker(spy, RedisCircuitBreaker(redis, "mcp:test")), limiter
        )
        asyncio.run(guarded("getProductByName", {"name": "苹果"}))
        asyncio.run(guarded("getProductByName", {"name": "苹果"}))
        result = asyncio.run(guarded("getProductByName", {"name": "苹果"}))

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == RATE_LIMITED_CODE
        assert result.error.is_retryable is False
        assert calls == ["getProductByName", "getProductByName"]  # third never hit upstream

    def test_under_limit_executes_and_passes_through(self) -> None:
        redis = _FakeRedis()
        clock = _FakeClock()
        limiter = RedisRateLimiter(redis, "mcp:test", limit=2, now=clock)

        async def spy(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.success(tool_version_id=tool_name, data={"ok": True})

        guarded = _with_rate_limit(
            _with_circuit_breaker(spy, RedisCircuitBreaker(redis, "mcp:test")), limiter
        )
        result = asyncio.run(guarded("getProductByName", {"name": "苹果"}))

        assert result.status == "SUCCEEDED"
        assert result.data == {"ok": True}

    def test_exposes_inner_executor_breaker_and_limiter(self) -> None:
        redis = _FakeRedis()
        clock = _FakeClock()
        limiter = RedisRateLimiter(redis, "mcp:test", limit=2, now=clock)
        breaker = RedisCircuitBreaker(redis, "mcp:test")

        async def spy(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.success(tool_version_id=tool_name, data={})

        guarded = _with_rate_limit(_with_circuit_breaker(spy, breaker), limiter)

        assert guarded.executor is spy
        assert guarded.breaker is breaker
        assert guarded.limiter is limiter


class TestDeleteSupplier:
    def test_delete_by_name_clears_indexes(self) -> None:
        name = _unique_supplier_name()
        created = _run(
            "addSuppliers",
            {
                "name": name,
                "regions": ["上海"],
                "status": "AVAILABLE",
                "idempotency_key": _m3_key(),
            },
        )
        supplier_id = created.data["supplier_id"]

        result = _run("deleteSupplierByName", {"name": name, "idempotency_key": _m3_key()})

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        assert SUPPLIER_BY_NAME.get(name) is None
        assert SUPPLIER_BY_ID.get(supplier_id) is None

    def test_delete_by_id_clears_indexes(self) -> None:
        name = _unique_supplier_name()
        created = _run(
            "addSuppliers",
            {
                "name": name,
                "regions": ["上海"],
                "status": "AVAILABLE",
                "idempotency_key": _m3_key(),
            },
        )
        supplier_id = created.data["supplier_id"]

        result = _run(
            "deleteSupplierById", {"supplier_id": supplier_id, "idempotency_key": _m3_key()}
        )

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        assert SUPPLIER_BY_NAME.get(name) is None

    def test_delete_unknown_is_not_found(self) -> None:
        result = _run(
            "deleteSupplierByName", {"name": "不存在的供应商", "idempotency_key": _m3_key()}
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_NOT_FOUND"

    def test_requires_idempotency_key(self) -> None:
        result = _run("deleteSupplierById", {"supplier_id": 3})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "IDEMPOTENCY_KEY_REQUIRED"
