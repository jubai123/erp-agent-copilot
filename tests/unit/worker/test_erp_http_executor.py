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


class TestUnknownTool:
    def test_unknown_tool_fails_closed(self) -> None:
        with respx.mock:
            result = _call("cancelOrder", {"order_id": 1})

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "UNKNOWN_TOOL"
        assert result.error.is_retryable is False


def _settings(*, erp_api_base_url: str) -> Settings:
    return Settings(
        database_url="postgresql://localhost/test",
        llm_api_key="sk-test",
        erp_api_base_url=erp_api_base_url,
        erp_api_key="cloud-key",
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
