"""Integration: the cloud-ERP MCP server exposes the real ERP contract over MCP.

Serves apps.mcp_gateway.erp_mcp_server (an MCP SDK 2.0 MCPServer backed by
:func:`apps.worker.executor.build_erp_http_executor`) under uvicorn on a free
port, then drives it with :class:`MCPGatewayConnection` over the real Streamable
HTTP transport. respx mocks the cloud ERP's httpx calls, so the full pipeline —
MCP client -> MCP server -> real cloud executor -> normalized ToolResult -> MCP
response — runs with no real network. The MCP client->server leg uses the SDK's
own ``httpx2`` client, which respx does not patch; only the executor's
``httpx.AsyncClient`` calls are intercepted.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
import uvicorn

from apps.mcp_gateway.erp_mcp_server import build_erp_mcp_app
from erp_copilot.infrastructure.config import Settings
from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard
from erp_copilot.tools.mcp_gateway import MCPGatewayConnection

_BASE_URL = "http://121.43.198.13:8080"
_API_KEY = "test-key"

_ALL_TOOLS = [
    "getProductByName",
    "getProductById",
    "getProductSubstitutes",
    "getProductSubstitutesByName",
    "getBatchProductByProductIds",
    "getSupplierByStatus",
    "querySuppliersByDeliveryRegion",
    "getSupplierByName",
    "getSupplierById",
    "createOrder",
    "updateOrderStatus",
    "cancelOrder",
    "getOrderByOrderId",
    "getOrdersBySupplierId",
    "getByProductId",
    "getByOrderStatus",
    "getByTimeRange",
]
_READ_TOOLS = [
    "getProductByName",
    "getProductById",
    "getProductSubstitutes",
    "getProductSubstitutesByName",
    "getBatchProductByProductIds",
    "getSupplierByStatus",
    "querySuppliersByDeliveryRegion",
    "getSupplierByName",
    "getSupplierById",
    "getOrderByOrderId",
    "getOrdersBySupplierId",
    "getByProductId",
    "getByOrderStatus",
    "getByTimeRange",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(base_url: str) -> None:
    """Poll the erp MCP server's /health route until it answers 200.

    trust_env=False: the dev machine's system proxy (Clash on 127.0.0.1:7890)
    otherwise intercepts the loopback request and returns 502 — the same proxy
    the MCP client already bypasses (trust_env=False on its httpx2 client).
    """
    last_error: Exception | None = None
    with httpx.Client(trust_env=False) as client:
        for _ in range(100):
            try:
                if client.get(f"{base_url}/health", timeout=1).status_code == 200:
                    return
            except Exception as exc:  # pragma: no cover - transient until boot
                last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"erp MCP server did not become ready: {last_error!r}")


def _localhost_guard(port: int) -> SSRFGuard:
    """Guard that admits http://127.0.0.1:<port> — the erp MCP server's URL."""
    return SSRFGuard(
        SSRFConfig(
            allowed_schemes=frozenset({"http"}),
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({port}),
            trusted_internal_hosts=frozenset({"127.0.0.1"}),
        )
    )


def _run_in_one_loop(coro) -> Any:
    """Drive the whole connection lifecycle in a single event loop.

    The MCP client's anyio task group is loop-bound, so connect() and
    disconnect() must run in the same asyncio.run; a fresh loop per call would
    break teardown.
    """
    return asyncio.run(coro)


def _settings(*, create_order_enabled: bool = True) -> Settings:
    return Settings(
        database_url="postgresql://localhost/test",
        llm_api_key="sk-test",
        erp_api_base_url=_BASE_URL,
        erp_api_key=_API_KEY,
        erp_mcp_create_order_enabled=create_order_enabled,
    )


def _serve(settings: Settings) -> Iterator[str]:
    """Serve the erp MCP server on a free port; yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(
        build_erp_mcp_app(settings), host="127.0.0.1", port=port, log_level="error"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base_url)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def erp_mcp_url() -> Iterator[str]:
    yield from _serve(_settings(create_order_enabled=True))


@pytest.fixture()
def erp_mcp_url_gated() -> Iterator[str]:
    yield from _serve(_settings(create_order_enabled=False))


def _connect(url: str) -> MCPGatewayConnection:
    port = int(url.rsplit("/mcp", 1)[0].rsplit(":", 1)[1])
    return MCPGatewayConnection(f"{url}/mcp", guard=_localhost_guard(port))


class TestRealTransportRoundTrip:
    """Acceptance: real cloud executor through MCP, all five tools."""

    def test_health_route(self, erp_mcp_url: str) -> None:
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"{erp_mcp_url}/health", timeout=5)
        assert response.status_code == 200

    def test_all_five_tools_over_mcp_with_real_executor(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[
            list[str],
            Any,
            Any,
            Any,
            Any,
            MCPGatewayConnection,
        ]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                tools = await conn.list_tools()
                product = await conn.execute_tool("getProductByName", {"name": "苹果"})
                suppliers = await conn.execute_tool(
                    "getSupplierByStatus", {"status": "AVAILABLE"}
                )
                order = await conn.execute_tool(
                    "createOrder",
                    {
                        "idempotency_key": "k-123",
                        "product_id": 10094,
                        "quantity": 20,
                        "supplier_id": 2987,
                        "region": "上海",
                    },
                )
                readback = await conn.execute_tool(
                    "getOrderByOrderId", {"order_id": "120562"}
                )
                return tools, product, suppliers, order, readback, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            product_route = respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 10094,
                        "name": "苹果",
                        "description": "新鲜苹果",
                        "price": 10.0,
                        "quantityInStock": 100,
                        "substituteProductId": None,
                    },
                )
            )
            supplier_route = respx.get(f"{_BASE_URL}/suppliers/getSupplierByStatus").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "supplierId": 2987,
                            "name": "京东222",
                            "phone": "12345678",
                            "address": "上海",
                            "deliveryAreas": ["上海"],
                            "rating": 100.0,
                            "status": "InUse",
                        }
                    ],
                )
            )
            create_route = respx.post(f"{_BASE_URL}/orders/createOrder").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": 120562,
                        "orderTime": "2026-08-15T16:31:29Z",
                        "quantity": 20,
                        "amount": 200.0,
                        "status": "CREATED",
                        "supplierId": 2987,
                        "productId": 10094,
                        "orderRegion": "上海",
                    },
                )
            )
            readback_route = respx.post(f"{_BASE_URL}/orders/getOrderByOrderId").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "order": {
                            "id": 120562,
                            "orderTime": "2026-08-15T16:31:29Z",
                            "quantity": 1,
                            "amount": 5.0,
                            "status": "已下单",
                            "supplierId": 2987,
                            "productId": 10094,
                            "orderRegion": "上海",
                        },
                        "supplier": {"supplierId": 2987, "name": "京东222"},
                    },
                )
            )
            tools, product, suppliers, order, readback, conn = _run_in_one_loop(
                exercise()
            )

        # Torn down inside the loop: the session and transport are closed.
        assert conn.is_connected is False
        assert tools == _ALL_TOOLS
        # getProductByName: camelCase normalized back to the simulator's shape.
        assert product.structured_content == {
            "product_id": 10094,
            "name": "苹果",
            "description": "新鲜苹果",
            "price": 10.0,
            "stock": 100,
        }
        assert product_route.calls.last.request.headers["X-API-Key"] == _API_KEY
        assert json.loads(product_route.calls.last.request.content) == {"name": "苹果"}
        # getSupplierByStatus: status remapped to the cloud enum.
        assert suppliers.structured_content["supplier_id"] == 2987
        assert suppliers.structured_content["suppliers"][0]["regions"] == ["上海"]
        assert dict(supplier_route.calls.last.request.url.params) == {"status": "InUse"}
        # createOrder: normalized flat order; idempotency_key dropped (no cloud field).
        assert order.structured_content == {
            "order_id": 120562,
            "product_id": 10094,
            "quantity": 20,
            "supplier_id": 2987,
            "region": "上海",
            "amount": 200.0,
            "status": "CREATED",
            "created_at": "2026-08-15T16:31:29Z",
        }
        create_body = json.loads(create_route.calls.last.request.content)
        assert b"idempotency_key" not in create_route.calls.last.request.content
        assert create_body == {
            "quantity": 20,
            "productId": 10094,
            "supplierId": 2987,
            "orderRegion": "上海",
        }
        # getOrderByOrderId: nested {order, supplier} envelope unwrapped.
        assert readback.structured_content["order_id"] == 120562
        assert readback.structured_content["region"] == "上海"
        assert readback.structured_content["status"] == "已下单"
        assert json.loads(readback_route.calls.last.request.content) == {"orderId": "120562"}


class TestCreateOrderGate:
    """The write tool is only advertised when the gate flag is on."""

    def test_create_order_omitted_when_gated_off(self, erp_mcp_url_gated: str) -> None:
        async def exercise() -> tuple[list[str], Any, MCPGatewayConnection]:
            conn = _connect(erp_mcp_url_gated)
            await conn.connect()
            try:
                tools = await conn.list_tools()
                err = await conn.execute_tool(
                    "createOrder",
                    {
                        "idempotency_key": "k-123",
                        "product_id": 10094,
                        "quantity": 20,
                        "supplier_id": 2987,
                        "region": "上海",
                    },
                )
                return tools, err, conn
            finally:
                await conn.disconnect()

        tools, err, conn = _run_in_one_loop(exercise())

        assert tools == _READ_TOOLS
        assert err.is_error is True
        assert "Unknown tool" in err.content[0].text


class TestFailureMapping:
    """Cloud executor failures surface as MCP is_error results."""

    def test_product_not_found_is_error(self, erp_mcp_url: str) -> None:
        async def exercise() -> Any:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                return await conn.execute_tool("getProductByName", {"name": "香蕉"})
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _run_in_one_loop(exercise())

        assert result.is_error is True
        assert "PRODUCT_NOT_FOUND" in result.content[0].text

    def test_create_order_business_failure_is_error(self, erp_mcp_url: str) -> None:
        async def exercise() -> Any:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                return await conn.execute_tool(
                    "createOrder",
                    {
                        "idempotency_key": "k-456",
                        "product_id": 999,
                        "quantity": 20,
                        "supplier_id": 3,
                        "region": "上海",
                    },
                )
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/createOrder").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": -1, "status": "不存在该ID的产品，所以无法创建订单"},
                )
            )
            result = _run_in_one_loop(exercise())

        assert result.is_error is True
        assert "ORDER_CREATE_FAILED" in result.content[0].text
        assert "不存在该ID的产品" in result.content[0].text

    def test_upstream_5xx_is_error(self, erp_mcp_url: str) -> None:
        async def exercise() -> Any:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                return await conn.execute_tool("getProductByName", {"name": "苹果"})
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductByName").mock(
                return_value=httpx.Response(503, json={"message": "down"})
            )
            result = _run_in_one_loop(exercise())

        # is_retryable is not carried across the MCP boundary — a documented
        # limitation: the client sees is_error=True and maps it to a permanent
        # failure. The error_code is still visible for diagnostics.
        assert result.is_error is True
        assert "UPSTREAM_503" in result.content[0].text


class TestProductReadTools:
    """The four product read tools round-trip over real MCP + respx cloud."""

    def test_by_id_normalizes_single_product(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[dict[str, Any], MCPGatewayConnection]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                product = await conn.execute_tool("getProductById", {"product_id": 10094})
                return product.structured_content, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            route = respx.post(f"{_BASE_URL}/products/getProductById").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "productId": 10094,
                        "name": "苹果",
                        "description": "新鲜苹果",
                        "price": 10.0,
                        "quantityInStock": 100,
                        "substituteProductId": None,
                    },
                )
            )
            product, conn = _run_in_one_loop(exercise())

        assert conn.is_connected is False
        assert product == {
            "product_id": 10094,
            "name": "苹果",
            "description": "新鲜苹果",
            "price": 10.0,
            "stock": 100,
        }
        assert json.loads(route.calls.last.request.content) == {"productId": 10094}

    def test_substitutes_by_name_round_trips(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[dict[str, Any], MCPGatewayConnection]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                subs = await conn.execute_tool("getProductSubstitutesByName", {"name": "苹果"})
                return subs.structured_content, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getProductSubstitutesByName").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "productId": 2987,
                            "name": "香蕉",
                            "description": "进口香蕉",
                            "price": 8.0,
                            "quantityInStock": 50,
                            "substituteProductId": None,
                        }
                    ],
                )
            )
            subs, conn = _run_in_one_loop(exercise())

        assert conn.is_connected is False
        assert subs["substitutes"] == [
            {
                "product_id": 2987,
                "name": "香蕉",
                "description": "进口香蕉",
                "price": 8.0,
                "stock": 50,
            }
        ]

    def test_batch_query_round_trips(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[dict[str, Any], MCPGatewayConnection]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                batch = await conn.execute_tool(
                    "getBatchProductByProductIds", {"start_id": 10090, "end_id": 10095}
                )
                return batch.structured_content, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.post(f"{_BASE_URL}/products/getBatchProductByProductIds").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "productId": 10094,
                            "name": "苹果",
                            "description": "新鲜苹果",
                            "price": 10.0,
                            "quantityInStock": 100,
                            "substituteProductId": None,
                        }
                    ],
                )
            )
            batch, conn = _run_in_one_loop(exercise())

        assert conn.is_connected is False
        assert [p["product_id"] for p in batch["products"]] == [10094]


class TestSupplierReadTools:
    """The two single-supplier read tools round-trip over real MCP + respx cloud."""

    def test_by_name_round_trips(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[dict[str, Any], MCPGatewayConnection]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                supplier = await conn.execute_tool("getSupplierByName", {"name": "京东222"})
                return supplier.structured_content, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            route = respx.get(f"{_BASE_URL}/suppliers/getSupplierByName").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "supplierId": 2987,
                        "name": "京东222",
                        "phone": "12345678",
                        "address": "上海",
                        "deliveryAreas": ["上海"],
                        "rating": 100.0,
                        "status": "InUse",
                    },
                )
            )
            supplier, conn = _run_in_one_loop(exercise())

        assert conn.is_connected is False
        # A single-supplier lookup returns a flat dict (not the set envelope).
        assert supplier == {
            "supplier_id": 2987,
            "name": "京东222",
            "regions": ["上海"],
            "status": "AVAILABLE",
            "rating": 100.0,
        }
        assert dict(route.calls.last.request.url.params) == {"supplierName": "京东222"}

    def test_by_id_round_trips(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[dict[str, Any], MCPGatewayConnection]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                supplier = await conn.execute_tool("getSupplierById", {"supplier_id": 2987})
                return supplier.structured_content, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.get(f"{_BASE_URL}/suppliers/getSupplierById/2987").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "supplierId": 2987,
                        "name": "京东222",
                        "phone": "12345678",
                        "address": "上海",
                        "deliveryAreas": ["上海"],
                        "rating": 100.0,
                        "status": "InUse",
                    },
                )
            )
            supplier, conn = _run_in_one_loop(exercise())

        assert conn.is_connected is False
        assert supplier["supplier_id"] == 2987
        assert supplier["name"] == "京东222"
        assert supplier["regions"] == ["上海"]

    def test_not_found_surfaces_is_error(self, erp_mcp_url: str) -> None:
        async def exercise() -> Any:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                return await conn.execute_tool("getSupplierById", {"supplier_id": 999})
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.get(f"{_BASE_URL}/suppliers/getSupplierById/999").mock(
                return_value=httpx.Response(200, json={})
            )
            result = _run_in_one_loop(exercise())

        assert result.is_error is True
        assert "SUPPLIER_NOT_FOUND" in result.content[0].text


class TestOrderQueryTools:
    """The four order set-queries round-trip over real MCP + respx cloud.

    The cloud answers a bare Order array; the executor wraps it in
    {"orders": [...]}, and an empty array stays a successful "no match".
    """

    def test_get_orders_by_supplier_round_trips(self, erp_mcp_url: str) -> None:
        async def exercise() -> tuple[dict[str, Any], MCPGatewayConnection]:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                orders = await conn.execute_tool("getOrdersBySupplierId", {"supplier_id": 2987})
                return orders.structured_content, conn
            finally:
                await conn.disconnect()

        with respx.mock:
            route = respx.post(f"{_BASE_URL}/orders/getOrdersBySupplierId").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": 120562,
                            "orderTime": "2026-08-15T16:31:29.518+00:00",
                            "quantity": 1,
                            "amount": 5.0,
                            "status": "已下单",
                            "supplierId": 2987,
                            "productId": 10094,
                            "orderRegion": "上海",
                        }
                    ],
                )
            )
            orders, conn = _run_in_one_loop(exercise())

        assert conn.is_connected is False
        assert orders["orders"][0]["order_id"] == 120562
        assert orders["orders"][0]["supplier_id"] == 2987
        assert orders["orders"][0]["status"] == "已下单"  # cloud status passthrough
        assert json.loads(route.calls.last.request.content) == {"supplierId": 2987}

    def test_empty_array_is_success(self, erp_mcp_url: str) -> None:
        async def exercise() -> Any:
            conn = _connect(erp_mcp_url)
            await conn.connect()
            try:
                return await conn.execute_tool("getByOrderStatus", {"status": "DELIVERED"})
            finally:
                await conn.disconnect()

        with respx.mock:
            respx.post(f"{_BASE_URL}/orders/getByOrderStatus").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = _run_in_one_loop(exercise())

        assert result.is_error is False
        assert result.structured_content == {"orders": []}
