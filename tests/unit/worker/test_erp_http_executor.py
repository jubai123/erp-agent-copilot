"""Unit tests for the cloud ERP HTTP executor (V5 calling convention).

The worker's executor seam is async ``(tool_name, arguments) -> ToolResult``.
``build_erp_http_executor`` resolves tool calls against the real cloud ERP API
at ``ERP_API_BASE_URL`` using the V5 request shape (X-API-Key header, GET query
params / POST JSON body) and normalizes the cloud's camelCase responses back to
the exact snake_case ``ToolResult.data`` shapes the in-process simulator
executor produces — so the deterministic planner's ``argument_sources``
(step:s1 product_id, step:s2 supplier_id) resolve identically.

Like the simulator executor tests, these are driven with asyncio.run (the suite
has no asyncio-mode config). respx mocks httpx so no network is touched.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import respx

from apps.worker.executor import (
    build_erp_http_executor,
    erp_simulator_executor,
    resolve_erp_executor,
)
from apps.worker.mcp_executor import MCPToolExecutor
from erp_copilot.infrastructure.config import Settings
from erp_copilot.tools.tool_result import ToolResult

_BASE_URL = "http://121.43.198.13:8080"
_API_KEY = "test-key"


def _call(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    executor = build_erp_http_executor(_BASE_URL, _API_KEY)
    return asyncio.run(executor(tool_name, arguments))


def _assert_body(request: httpx.Request, expected: dict[str, Any]) -> None:
    assert json.loads(request.content) == expected


class TestGetProductByName:
    def test_normalizes_product_and_sends_key_header(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 1,
                        "name": "苹果",
                        "description": "新鲜苹果",
                        "price": 10.0,
                        "quantityInStock": 100,
                        "substituteProductId": None,
                    },
                )
            )
            result = _call("getProductByName", {"name": "苹果"})

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "product_id": 1,
            "name": "苹果",
            "description": "新鲜苹果",
            "price": 10.0,
            "stock": 100,
        }
        request = route.calls.last.request
        assert request.headers["X-API-Key"] == _API_KEY
        _assert_body(request, {"name": "苹果"})

    def test_empty_response_is_permanent_not_found(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("getProductByName", {"name": "香蕉"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"
        assert result.error.is_retryable is False


class TestGetSupplierByStatus:
    def test_maps_status_query_and_normalizes_suppliers(self) -> None:
        with respx.mock:
            route = respx.get(f"{_BASE_URL}/suppliers/getSupplierByStatus").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "supplierId": 10,
                            "name": "华东物流",
                            "phone": "021-123",
                            "address": "上海",
                            # The live cloud API returns deliveryAreas as plain
                            # region strings (not {region, ...} objects).
                            "deliveryAreas": ["上海", "南京"],
                            "rating": 4.8,
                            "status": "InUse",
                        },
                        {
                            "supplierId": 11,
                            "name": "华南物流",
                            "phone": "020-456",
                            "address": "广州",
                            "deliveryAreas": ["广州"],
                            "rating": 4.2,
                            "status": "DisUse",
                        },
                    ],
                )
            )
            result = _call("getSupplierByStatus", {"status": "AVAILABLE"})

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "suppliers": [
                {
                    "supplier_id": 10,
                    "name": "华东物流",
                    "regions": ["上海", "南京"],
                    "status": "AVAILABLE",
                    "rating": 4.8,
                },
                {
                    "supplier_id": 11,
                    "name": "华南物流",
                    "regions": ["广州"],
                    "status": "UNAVAILABLE",
                    "rating": 4.2,
                },
            ],
            "supplier_id": 10,
        }
        assert dict(route.calls.last.request.url.params) == {"status": "InUse"}

    def test_tolerates_spec_object_form_delivery_areas(self) -> None:
        # The OpenAPI spec documents deliveryAreas as {region, ...} objects;
        # the live API returns plain strings. The normalizer accepts both.
        with respx.mock:
            respx.get(f"{_BASE_URL}/suppliers/getSupplierByStatus").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "supplierId": 10,
                            "name": "华东物流",
                            "phone": "021-123",
                            "address": "上海",
                            "deliveryAreas": [
                                {"region": "上海", "distance": 10.0, "price": 1.2, "paths": []}
                            ],
                            "rating": 4.8,
                            "status": "InUse",
                        }
                    ],
                )
            )
            result = _call("getSupplierByStatus", {"status": "AVAILABLE"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["suppliers"][0]["regions"] == ["上海"]


class TestQuerySuppliersByDeliveryRegion:
    def test_posts_region_and_normalizes_suppliers(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/suppliers/querySuppliersByDeliveryRegion").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "supplierId": 10,
                            "name": "华东物流",
                            "phone": "021-123",
                            "address": "上海",
                            "deliveryAreas": ["上海"],
                            "rating": 4.8,
                            "status": "InUse",
                        }
                    ],
                )
            )
            result = _call("querySuppliersByDeliveryRegion", {"region": "上海"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["supplier_id"] == 10
        assert result.data["suppliers"][0]["regions"] == ["上海"]
        _assert_body(route.calls.last.request, {"region": "上海"})


class TestGetSupplierByName:
    def test_gets_query_param_and_normalizes_supplier(self) -> None:
        with respx.mock:
            route = respx.get(f"{_BASE_URL}/suppliers/getSupplierByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "supplierId": 10,
                        "name": "华东物流",
                        "phone": "021-123",
                        "address": "上海",
                        "deliveryAreas": ["上海", "南京"],
                        "rating": 4.8,
                        "status": "InUse",
                    },
                )
            )
            result = _call("getSupplierByName", {"name": "华东物流"})

        assert result.status == "SUCCEEDED"
        # A single-supplier lookup returns the flat supplier dict (mirrors the
        # simulator's _supplier_data), not the {"suppliers": [...]} envelope.
        assert result.data == {
            "supplier_id": 10,
            "name": "华东物流",
            "regions": ["上海", "南京"],
            "status": "AVAILABLE",
            "rating": 4.8,
        }
        assert dict(route.calls.last.request.url.params) == {"supplierName": "华东物流"}
        assert route.calls.last.request.headers["X-API-Key"] == _API_KEY

    def test_empty_response_is_permanent_not_found(self) -> None:
        with respx.mock:
            respx.get(f"{_BASE_URL}/suppliers/getSupplierByName").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("getSupplierByName", {"name": "不存在"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_NOT_FOUND"
        assert result.error.is_retryable is False


class TestGetSupplierById:
    def test_gets_path_id_and_normalizes_supplier(self) -> None:
        with respx.mock:
            route = respx.get(f"{_BASE_URL}/suppliers/getSupplierById/10").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "supplierId": 10,
                        "name": "华东物流",
                        "phone": "021-123",
                        "address": "上海",
                        "deliveryAreas": ["上海"],
                        "rating": 4.8,
                        "status": "InUse",
                    },
                )
            )
            result = _call("getSupplierById", {"supplier_id": 10})

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "supplier_id": 10,
            "name": "华东物流",
            "regions": ["上海"],
            "status": "AVAILABLE",
            "rating": 4.8,
        }
        assert route.calls.last.request.headers["X-API-Key"] == _API_KEY

    def test_empty_response_is_permanent_not_found(self) -> None:
        with respx.mock:
            respx.get(f"{_BASE_URL}/suppliers/getSupplierById/999").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("getSupplierById", {"supplier_id": 999})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_NOT_FOUND"
        assert result.error.is_retryable is False


class TestCreateOrder:
    def test_drops_idempotency_key_and_maps_fields(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/createOrder").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "orderTime": "2026-08-15T10:00:00Z",
                        "quantity": 20,
                        "amount": 200.0,
                        "status": "CREATED",
                        "supplierId": 3,
                        "productId": 1,
                        "orderRegion": "上海",
                    },
                )
            )
            result = _call(
                "createOrder",
                {
                    "product_id": 1,
                    "supplier_id": 3,
                    "quantity": 20,
                    "region": "上海",
                    "idempotency_key": "k-123",
                },
            )

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "order_id": 7,
            "product_id": 1,
            "quantity": 20,
            "supplier_id": 3,
            "region": "上海",
            "amount": 200.0,
            "status": "CREATED",
            "created_at": "2026-08-15T10:00:00Z",
        }
        request = route.calls.last.request
        _assert_body(
            request,
            {"quantity": 20, "productId": 1, "supplierId": 3, "orderRegion": "上海"},
        )
        assert b"idempotency_key" not in request.content

    def test_business_failure_id_minus_one_is_permanent_failed(self) -> None:
        # A structurally valid but business-invalid createOrder returns HTTP 200
        # with the sentinel {"id": -1} plus the cloud's reason in "status" — not
        # a 4xx. Treating it as success would fabricate an order that never
        # exists, so it must surface as ORDER_CREATE_FAILED.
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/createOrder").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": -1, "status": "不存在该ID的产品，所以无法创建订单"},
                )
            )
            result = _call(
                "createOrder",
                {
                    "product_id": 999,
                    "supplier_id": 3,
                    "quantity": 20,
                    "region": "上海",
                    "idempotency_key": "k-456",
                },
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_CREATE_FAILED"
        assert result.error.is_retryable is False
        assert "不存在该ID的产品" in result.error.error_message


class TestGetOrderByOrderId:
    def test_posts_order_id_and_normalizes_order(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/getOrderByOrderId").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "orderTime": "2026-08-15T10:00:00Z",
                        "quantity": 20,
                        "amount": 200.0,
                        "status": "CREATED",
                        "supplierId": 3,
                        "productId": 1,
                        "orderRegion": "上海",
                    },
                )
            )
            result = _call("getOrderByOrderId", {"order_id": 7})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["order_id"] == 7
        assert result.data["product_id"] == 1
        assert result.data["region"] == "上海"
        _assert_body(route.calls.last.request, {"orderId": 7})

    def test_nested_order_envelope_is_normalized(self) -> None:
        # The real cloud wraps the order in {"order": {...}} with a parallel
        # {"supplier": {...}} object — the flat shape the original unit test
        # assumed does not exist in the wild. Reading back an existing order
        # must unwrap the envelope and normalize the inner order.
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getOrderByOrderId").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "order": {
                            "id": 120562,
                            "orderTime": "2026-08-15T16:31:29.518+00:00",
                            "quantity": 1,
                            "amount": 5.0,
                            "status": "已下单",
                            "supplierId": 2987,
                            "productId": 10094,
                            "orderRegion": "上海",
                        },
                        "supplier": {
                            "supplierId": 2987,
                            "name": "京东222",
                            "phone": "12345678",
                            "address": "上海某某地址",
                            "deliveryAreas": ["上海"],
                            "rating": 100.0,
                            "status": "InUse",
                        },
                    },
                )
            )
            result = _call("getOrderByOrderId", {"order_id": 120562})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["order_id"] == 120562
        assert result.data["product_id"] == 10094
        assert result.data["supplier_id"] == 2987
        assert result.data["region"] == "上海"
        assert result.data["status"] == "已下单"

    def test_empty_response_is_permanent_not_found(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getOrderByOrderId").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("getOrderByOrderId", {"order_id": 999})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_NOT_FOUND"
        assert result.error.is_retryable is False

    def test_redirect_302_is_permanent_not_found(self) -> None:
        # The cloud's "resource not found" fallback is a 302 to /login (Spring
        # Security style), not a 404 — a nonexistent order id produces it. The
        # redirect body is an HTML login page, so it must be intercepted before
        # JSON parsing.
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getOrderByOrderId").mock(
                return_value=httpx.Response(
                    302,
                    headers={"location": "/login"},
                    text="<html>Redirecting...</html>",
                )
            )
            result = _call("getOrderByOrderId", {"order_id": 999})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_NOT_FOUND"
        assert result.error.is_retryable is False


class TestOrderQueryTools:
    """The four order set-queries POST the V5 body and normalize the cloud's
    bare Order array into the executor's {"orders": [...]} shape. Unlike the
    single lookups, an empty array is a legal business answer — success, not
    ORDER_NOT_FOUND."""

    def test_get_orders_by_supplier_posts_and_normalizes(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/getOrdersBySupplierId").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": 7,
                            "orderTime": "2026-08-15T10:00:00Z",
                            "quantity": 20,
                            "amount": 200.0,
                            "status": "CREATED",
                            "supplierId": 3,
                            "productId": 1,
                            "orderRegion": "上海",
                        }
                    ],
                )
            )
            result = _call("getOrdersBySupplierId", {"supplier_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "orders": [
                {
                    "order_id": 7,
                    "product_id": 1,
                    "quantity": 20,
                    "supplier_id": 3,
                    "region": "上海",
                    "amount": 200.0,
                    "status": "CREATED",
                    "created_at": "2026-08-15T10:00:00Z",
                }
            ]
        }
        _assert_body(route.calls.last.request, {"supplierId": 3})

    def test_get_orders_by_supplier_empty_array_is_success(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getOrdersBySupplierId").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _call("getOrdersBySupplierId", {"supplier_id": 999})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_by_product_posts_and_normalizes(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/getByProductId").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": 8,
                            "orderTime": "2026-08-16T09:00:00Z",
                            "quantity": 5,
                            "amount": 50.0,
                            "status": "已下单",
                            "supplierId": 10,
                            "productId": 2,
                            "orderRegion": "广州",
                        }
                    ],
                )
            )
            result = _call("getByProductId", {"product_id": 2})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["orders"][0]["product_id"] == 2
        # The cloud order status is a passthrough (its own Chinese enum).
        assert result.data["orders"][0]["status"] == "已下单"
        _assert_body(route.calls.last.request, {"productId": 2})

    def test_get_by_product_empty_array_is_success(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getByProductId").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _call("getByProductId", {"product_id": 999})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_by_status_posts_and_normalizes(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/getByOrderStatus").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": 9,
                            "orderTime": "2026-08-17T08:00:00Z",
                            "quantity": 2,
                            "amount": 20.0,
                            "status": "已发货",
                            "supplierId": 3,
                            "productId": 1,
                            "orderRegion": "上海",
                        }
                    ],
                )
            )
            result = _call("getByOrderStatus", {"status": "SHIPPED"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["orders"][0]["status"] == "已发货"
        _assert_body(route.calls.last.request, {"status": "SHIPPED"})

    def test_get_by_status_empty_array_is_success(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getByOrderStatus").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _call("getByOrderStatus", {"status": "DELIVERED"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}

    def test_get_by_time_range_posts_and_normalizes(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/getByTimeRange").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": 10,
                            "orderTime": "2026-08-18T07:00:00Z",
                            "quantity": 3,
                            "amount": 30.0,
                            "status": "CREATED",
                            "supplierId": 3,
                            "productId": 1,
                            "orderRegion": "上海",
                        }
                    ],
                )
            )
            result = _call(
                "getByTimeRange", {"start_date": "2026-08-01", "end_date": "2026-08-31"}
            )

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["orders"][0]["order_id"] == 10
        _assert_body(
            route.calls.last.request, {"startDate": "2026-08-01", "endDate": "2026-08-31"}
        )

    def test_get_by_time_range_empty_array_is_success(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getByTimeRange").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _call("getByTimeRange", {"start_date": "2099-01-01", "end_date": "2099-12-31"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"orders": []}


class TestErrorMapping:
    def test_network_error_is_retryable(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                side_effect=httpx.ConnectError("boom")
            )
            result = _call("getProductByName", {"name": "苹果"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_UNAVAILABLE"
        assert result.error.is_retryable is True

    def test_timeout_is_retryable(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                side_effect=httpx.ReadTimeout("slow")
            )
            result = _call("getProductByName", {"name": "苹果"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "TIMEOUT"
        assert result.error.is_retryable is True

    def test_http_4xx_is_permanent(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(404, json={"message": "nope"})
            )
            result = _call("getProductByName", {"name": "苹果"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_404"
        assert result.error.is_retryable is False

    def test_http_5xx_is_retryable(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(503, json={"message": "down"})
            )
            result = _call("getProductByName", {"name": "苹果"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UPSTREAM_503"
        assert result.error.is_retryable is True

    def test_non_json_response_is_permanent(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(200, text="not json")
            )
            result = _call("getProductByName", {"name": "苹果"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "INVALID_RESPONSE"
        assert result.error.is_retryable is False

    def test_malformed_base_url_fails_not_raises(self) -> None:
        # The executor must never raise (the verify node needs a StepResult per
        # step); a misconfigured ERP_API_BASE_URL is an operator error that
        # surfaces as a clean ToolResult failure, not a crash. Which error code
        # httpx maps the bad URL to is implementation-dependent.
        executor = build_erp_http_executor("not a valid url", _API_KEY)
        result = asyncio.run(executor("getProductByName", {"name": "苹果"}))

        assert result.status == "FAILED"
        assert result.error is not None


class TestUpdateOrderStatus:
    def test_puts_new_status_and_normalizes_order(self) -> None:
        with respx.mock:
            route = respx.put(f"{_BASE_URL}/orders/updateOrderStatus").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "orderTime": "2026-08-15T10:00:00Z",
                        "quantity": 20,
                        "amount": 200.0,
                        "status": "SHIPPED",
                        "supplierId": 3,
                        "productId": 1,
                        "orderRegion": "上海",
                    },
                )
            )
            result = _call(
                "updateOrderStatus",
                {"order_id": 7, "status": "SHIPPED", "idempotency_key": "k-upd"},
            )

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "order_id": 7,
            "product_id": 1,
            "quantity": 20,
            "supplier_id": 3,
            "region": "上海",
            "amount": 200.0,
            "status": "SHIPPED",
            "created_at": "2026-08-15T10:00:00Z",
        }
        request = route.calls.last.request
        _assert_body(request, {"orderId": 7, "newStatus": "SHIPPED"})
        assert b"idempotency_key" not in request.content

    def test_business_failure_sentinel_is_permanent_failed(self) -> None:
        with respx.mock:
            respx.put(f"{_BASE_URL}/orders/updateOrderStatus").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": -1, "status": "订单已完成，无法修改状态"},
                )
            )
            result = _call(
                "updateOrderStatus",
                {"order_id": 7, "status": "SHIPPED", "idempotency_key": "k-upd-2"},
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_UPDATE_FAILED"
        assert result.error.is_retryable is False
        assert "订单已完成" in result.error.error_message


class TestCancelOrder:
    def test_deletes_order_and_normalizes_order(self) -> None:
        with respx.mock:
            route = respx.delete(f"{_BASE_URL}/orders/cancelOrder").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "orderTime": "2026-08-15T10:00:00Z",
                        "quantity": 20,
                        "amount": 200.0,
                        "status": "CANCELLED",
                        "supplierId": 3,
                        "productId": 1,
                        "orderRegion": "上海",
                    },
                )
            )
            result = _call("cancelOrder", {"order_id": 7, "idempotency_key": "k-cancel"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["status"] == "CANCELLED"
        request = route.calls.last.request
        _assert_body(request, {"orderId": 7})
        assert b"idempotency_key" not in request.content

    def test_business_failure_sentinel_is_permanent_failed(self) -> None:
        with respx.mock:
            respx.delete(f"{_BASE_URL}/orders/cancelOrder").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": -1, "status": "订单已完成，无法取消"},
                )
            )
            result = _call("cancelOrder", {"order_id": 7, "idempotency_key": "k-cancel-2"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "ORDER_CANCEL_FAILED"
        assert result.error.is_retryable is False
        assert "订单已完成" in result.error.error_message


class TestUnknownTool:
    def test_unknown_tool_fails_closed(self) -> None:
        with respx.mock:
            result = _call("noSuchTool", {"order_id": 1})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UNKNOWN_TOOL"
        assert result.error.is_retryable is False


def _settings(*, erp_api_base_url: str = "", mcp_server_url: str = "") -> Settings:
    return Settings(
        database_url="postgresql://localhost/test",
        llm_api_key="sk-test",
        erp_api_base_url=erp_api_base_url,
        erp_api_key="cloud-key",
        mcp_server_url=mcp_server_url,
    )


class TestResolveErpExecutor:
    def test_empty_base_url_keeps_simulator(self) -> None:
        assert resolve_erp_executor(_settings(erp_api_base_url="")) is erp_simulator_executor

    def test_set_base_url_returns_http_executor(self) -> None:
        executor = resolve_erp_executor(_settings(erp_api_base_url=_BASE_URL))
        assert callable(executor)
        assert executor is not erp_simulator_executor
        # The HTTP executor talks to the cloud over the configured base URL.
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 2,
                        "name": "香蕉",
                        "description": "新鲜香蕉",
                        "price": 5.0,
                        "quantityInStock": 50,
                    },
                )
            )
            result = asyncio.run(executor("getProductByName", {"name": "香蕉"}))
            assert result.status == "SUCCEEDED"
            assert route.calls.last.request.headers["X-API-Key"] == "cloud-key"

    def test_mcp_url_returns_mcp_executor(self) -> None:
        executor = resolve_erp_executor(_settings(mcp_server_url="http://localhost:8765/mcp"))

        # The MCP executor is wrapped in the circuit breaker and rate limiter;
        # the raw executor, breaker key and limiter name are exposed for
        # routing introspection.
        assert isinstance(executor.executor, MCPToolExecutor)
        assert executor.executor.connection.url == "http://localhost:8765/mcp"
        assert executor.breaker.name == "mcp:http://localhost:8765/mcp"
        assert executor.limiter.name == "mcp:http://localhost:8765/mcp"

    def test_mcp_url_takes_precedence_over_cloud_http(self) -> None:
        # MCP is the newest path: when both are set, the worker talks to the MCP
        # server, not the cloud ERP over HTTP.
        executor = resolve_erp_executor(
            _settings(erp_api_base_url=_BASE_URL, mcp_server_url="http://localhost:8765/mcp")
        )

        assert isinstance(executor.executor, MCPToolExecutor)
        assert executor.executor.connection.url == "http://localhost:8765/mcp"
        assert executor.limiter.name == "mcp:http://localhost:8765/mcp"


class TestGetProductById:
    def test_posts_id_and_normalizes_product(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getProductById").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 4,
                        "name": "电脑",
                        "description": "办公笔记本",
                        "price": 5000.0,
                        "quantityInStock": 15,
                        "substituteProductId": 5,
                    },
                )
            )
            result = _call("getProductById", {"product_id": 4})

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "product_id": 4,
            "name": "电脑",
            "description": "办公笔记本",
            "price": 5000.0,
            "stock": 15,
        }
        _assert_body(route.calls.last.request, {"productId": 4})

    def test_empty_response_is_permanent_not_found(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductById").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("getProductById", {"product_id": 99})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"
        assert result.error.is_retryable is False


class TestGetProductSubstitutes:
    def test_single_product_response_becomes_list(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getProductSubstitutes").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 2,
                        "name": "香蕉",
                        "description": "进口香蕉",
                        "price": 8.0,
                        "quantityInStock": 50,
                        "substituteProductId": None,
                    },
                )
            )
            result = _call("getProductSubstitutes", {"product_id": 1})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["substitutes"] == [
            {"product_id": 2, "name": "香蕉", "description": "进口香蕉", "price": 8.0, "stock": 50}
        ]
        _assert_body(route.calls.last.request, {"productId": 1})

    def test_bare_list_response_is_normalized(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductSubstitutes").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _call("getProductSubstitutes", {"product_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data == {"substitutes": []}

    def test_by_name_posts_name(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getProductSubstitutesByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 2,
                        "name": "香蕉",
                        "description": "进口香蕉",
                        "price": 8.0,
                        "quantityInStock": 50,
                        "substituteProductId": None,
                    },
                )
            )
            result = _call("getProductSubstitutesByName", {"name": "苹果"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["substitutes"][0]["name"] == "香蕉"
        _assert_body(route.calls.last.request, {"name": "苹果"})

    def test_empty_response_is_permanent_not_found(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductSubstitutesByName").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("getProductSubstitutesByName", {"name": "榴莲"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_NOT_FOUND"


class TestGetBatchProductByProductIds:
    def test_posts_range_and_normalizes_bare_array(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getBatchProductByProductIds").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "productId": 1,
                            "name": "苹果",
                            "description": "红富士",
                            "price": 10.0,
                            "quantityInStock": 100,
                            "substituteProductId": 2,
                        },
                        {
                            "productId": 2,
                            "name": "香蕉",
                            "description": "进口香蕉",
                            "price": 8.0,
                            "quantityInStock": 50,
                            "substituteProductId": None,
                        },
                    ],
                )
            )
            result = _call("getBatchProductByProductIds", {"start_id": 1, "end_id": 3})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert [p["product_id"] for p in result.data["products"]] == [1, 2]
        _assert_body(route.calls.last.request, {"startId": 1, "endId": 3})

    def test_empty_array_is_success(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getBatchProductByProductIds").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _call("getBatchProductByProductIds", {"start_id": 100, "end_id": 200})

        assert result.status == "SUCCEEDED"
        assert result.data == {"products": []}


class TestAddProduct:
    def test_posts_v5_body_and_normalizes_product(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/addProduct").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 10,
                        "name": "新商品",
                        "description": "新到货",
                        "price": 5.0,
                        "quantityInStock": 3,
                    },
                )
            )
            result = _call(
                "addProduct",
                {
                    "name": "新商品",
                    "description": "新到货",
                    "price": 5.0,
                    "quantity_in_stock": 3,
                    "idempotency_key": "k-add",
                },
            )

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "product_id": 10,
            "name": "新商品",
            "description": "新到货",
            "price": 5.0,
            "stock": 3,
        }
        request = route.calls.last.request
        _assert_body(
            request,
            {"name": "新商品", "description": "新到货", "price": 5.0, "quantityInStock": 3},
        )
        # idempotency_key is dropped: the cloud addProduct API has no such field.
        assert b"idempotency_key" not in request.content

    def test_missing_product_id_is_permanent_failure(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/addProduct").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call(
                "addProduct",
                {"name": "新商品", "price": 5.0, "quantity_in_stock": 3, "idempotency_key": "k"},
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_CREATE_FAILED"
        assert result.error.is_retryable is False


class TestUpdateProductDescription:
    def test_posts_v5_body_and_returns_success(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/updateProductDescription").mock(
                return_value=httpx.Response(200, json=True)
            )
            result = _call(
                "updateProductDescription",
                {"product_id": 1, "description": "新描述", "idempotency_key": "k-upd-desc"},
            )

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        _assert_body(route.calls.last.request, {"productId": 1, "description": "新描述"})
        assert b"idempotency_key" not in route.calls.last.request.content

    def test_false_response_is_permanent_failure(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/updateProductDescription").mock(
                return_value=httpx.Response(200, json=False)
            )
            result = _call(
                "updateProductDescription",
                {"product_id": 1, "description": "x", "idempotency_key": "k"},
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_UPDATE_FAILED"
        assert result.error.is_retryable is False


class TestUpdateProductSubstitutes:
    def test_posts_v5_body_and_returns_success(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/updateProductSubstitutes").mock(
                return_value=httpx.Response(200, json=True)
            )
            result = _call(
                "updateProductSubstitutes",
                {"product_id": 1, "substitute_name": "香蕉", "idempotency_key": "k-sub"},
            )

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        _assert_body(
            route.calls.last.request,
            {"productId": 1, "substituteName": "香蕉"},
        )

    def test_false_response_is_permanent_failure(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/products/updateProductSubstitutes").mock(
                return_value=httpx.Response(200, json=False)
            )
            result = _call(
                "updateProductSubstitutes",
                {"product_id": 1, "substitute_name": "香蕉", "idempotency_key": "k"},
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_UPDATE_FAILED"
        assert result.error.is_retryable is False


class TestRemoveProduct:
    def test_remove_by_name_deletes_and_normalizes_product(self) -> None:
        with respx.mock:
            route = respx.delete(f"{_BASE_URL}/products/removeProductByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 1,
                        "name": "苹果",
                        "description": "红富士",
                        "price": 10.0,
                        "quantityInStock": 100,
                    },
                )
            )
            result = _call("removeProductByName", {"name": "苹果", "idempotency_key": "k-rm1"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["product_id"] == 1
        _assert_body(route.calls.last.request, {"name": "苹果"})

    def test_remove_by_id_posts_id(self) -> None:
        with respx.mock:
            route = respx.delete(f"{_BASE_URL}/products/removeProductById").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 1,
                        "name": "苹果",
                        "description": "红富士",
                        "price": 10.0,
                        "quantityInStock": 100,
                    },
                )
            )
            result = _call("removeProductById", {"product_id": 1, "idempotency_key": "k-rm2"})

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["product_id"] == 1
        _assert_body(route.calls.last.request, {"productId": 1})

    def test_missing_product_id_is_permanent_failure(self) -> None:
        with respx.mock:
            respx.delete(f"{_BASE_URL}/products/removeProductByName").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("removeProductByName", {"name": "苹果", "idempotency_key": "k-rm3"})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "PRODUCT_REMOVE_FAILED"
        assert result.error.is_retryable is False


class TestAddSupplier:
    def test_posts_v5_body_and_normalizes_supplier(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/suppliers/addSuppliers").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "supplierId": 10,
                        "name": "华东物流",
                        "deliveryAreas": ["上海"],
                        "rating": 4.8,
                        "status": "InUse",
                    },
                )
            )
            result = _call(
                "addSuppliers",
                {
                    "name": "华东物流",
                    "regions": ["上海"],
                    "status": "AVAILABLE",
                    "idempotency_key": "k-add-sup",
                },
            )

        assert result.status == "SUCCEEDED"
        assert result.data == {
            "supplier_id": 10,
            "name": "华东物流",
            "regions": ["上海"],
            "status": "AVAILABLE",
            "rating": 4.8,
        }
        _assert_body(
            route.calls.last.request,
            {"name": "华东物流", "deliveryAreas": ["上海"], "status": "InUse"},
        )
        assert b"idempotency_key" not in route.calls.last.request.content

    def test_maps_v6_status_to_cloud_enum(self) -> None:
        with respx.mock:
            route = respx.post(f"{_BASE_URL}/suppliers/addSuppliers").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "supplierId": 11,
                        "name": "华北物流",
                        "deliveryAreas": ["北京"],
                        "rating": 4.5,
                        "status": "DisUse",
                    },
                )
            )
            result = _call(
                "addSuppliers",
                {
                    "name": "华北物流",
                    "regions": ["北京"],
                    "status": "UNAVAILABLE",
                    "idempotency_key": "k-add-sup2",
                },
            )

        assert result.status == "SUCCEEDED"
        assert result.data is not None
        assert result.data["status"] == "UNAVAILABLE"
        _assert_body(
            route.calls.last.request,
            {"name": "华北物流", "deliveryAreas": ["北京"], "status": "DisUse"},
        )

    def test_missing_supplier_id_is_permanent_failure(self) -> None:
        with respx.mock:
            respx.post(f"{_BASE_URL}/suppliers/addSuppliers").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call(
                "addSuppliers",
                {"name": "x", "regions": ["上海"], "status": "AVAILABLE", "idempotency_key": "k"},
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "SUPPLIER_CREATE_FAILED"
        assert result.error.is_retryable is False


class TestDeleteSupplier:
    def test_delete_by_name_success(self) -> None:
        with respx.mock:
            route = respx.delete(f"{_BASE_URL}/suppliers/deleteSupplierByName").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("deleteSupplierByName", {"name": "华东物流", "idempotency_key": "k-d1"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        _assert_body(route.calls.last.request, {"name": "华东物流"})

    def test_delete_by_id_success(self) -> None:
        with respx.mock:
            route = respx.delete(f"{_BASE_URL}/suppliers/deleteSupplierById").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _call("deleteSupplierById", {"supplier_id": 3, "idempotency_key": "k-d2"})

        assert result.status == "SUCCEEDED"
        assert result.data == {"success": True}
        _assert_body(route.calls.last.request, {"id": 3})
