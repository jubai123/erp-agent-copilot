"""Tool executors for the worker's agent graph.

Task 4.16: the worker drives the LangGraph, so the tool calls inside
execute_ready_steps resolve here — the same in-process simulator data the old
direct path used (apps.erp_simulator.data.products / .suppliers / .orders),
now behind the async (tool_name, arguments) -> ToolResult contract the
executor node expects. The scenario behavior (timeout / stock_insufficient /
supplier_unavailable) is preserved so the e2e scenario tests keep passing.

Write-path enablement: createOrder creates an order in the simulator's
in-memory store (delegating to apps.erp_simulator.data.orders), deducts stock,
and replays the existing order for the same idempotency_key — the at-most-once
behaviour the write DAGs rely on. getOrderByOrderId reads one back. Both mirror
the simulator HTTP routes (apps/erp_simulator/routes/orders.py) so the two
entry points stay consistent.

Cloud ERP (config-driven, simulator fallback): when ERP_API_BASE_URL is set,
``resolve_erp_executor`` returns ``build_erp_http_executor`` instead of the
in-process simulator — same tool contract, same normalized data shapes, but the
calls go to the real cloud ERP over HTTP using the V5 calling convention
(X-API-Key header; GET query params / POST JSON body). Cloud responses are
camelCase and normalized back to the snake_case shapes below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from apps.erp_simulator.data.orders import (
    cancel_order,
    create_order,
    get_by_id,
    get_by_idempotency_key,
    get_orders_by_product,
    get_orders_by_status,
    get_orders_by_supplier,
    get_orders_by_time_range,
    update_order_status,
)
from apps.erp_simulator.data.products import (
    PRODUCT_BY_ID,
    PRODUCT_BY_NAME,
    get_products_by_id_range,
    get_substitutes,
)
from apps.erp_simulator.data.suppliers import SUPPLIER_BY_ID, SUPPLIER_BY_NAME
from apps.erp_simulator.scenarios import get_scenario
from apps.worker.mcp_executor import build_mcp_executor
from erp_copilot.infrastructure.config import Settings
from erp_copilot.tools.tool_result import ToolResult

_TIMEOUT_ERROR = "Request timed out contacting ERP simulator"


async def erp_simulator_executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    """Execute one tool against the in-process ERP simulator data.

    The timeout scenario fails every tool (retryable); product/supplier/order
    tools add their own branches. Unknown tools fail closed (UNKNOWN_TOOL).
    """
    if get_scenario() == "timeout":
        return ToolResult.failure(
            tool_version_id=tool_name,
            error_code="TIMEOUT",
            error_message=_TIMEOUT_ERROR,
            is_retryable=True,
        )

    if tool_name == "getProductByName":
        return _get_product(arguments)
    if tool_name == "getProductById":
        return _get_product_by_id(arguments)
    if tool_name == "getProductSubstitutes":
        return _get_product_substitutes(arguments)
    if tool_name == "getProductSubstitutesByName":
        return _get_product_substitutes_by_name(arguments)
    if tool_name == "getBatchProductByProductIds":
        return _get_products_batch(arguments)
    if tool_name == "getSupplierByStatus":
        return _get_suppliers(arguments)
    if tool_name == "querySuppliersByDeliveryRegion":
        return _query_suppliers_by_region(arguments)
    if tool_name == "getSupplierByName":
        return _get_supplier_by_name(arguments)
    if tool_name == "getSupplierById":
        return _get_supplier_by_id(arguments)
    if tool_name == "createOrder":
        return _create_order(arguments)
    if tool_name == "updateOrderStatus":
        return _update_order_status(arguments)
    if tool_name == "cancelOrder":
        return _cancel_order(arguments)
    if tool_name == "getOrderByOrderId":
        return _get_order(arguments)
    if tool_name == "getOrdersBySupplierId":
        return _get_orders_by_supplier(arguments)
    if tool_name == "getByProductId":
        return _get_orders_by_product(arguments)
    if tool_name == "getByOrderStatus":
        return _get_orders_by_status(arguments)
    if tool_name == "getByTimeRange":
        return _get_orders_by_time_range(arguments)
    return ToolResult.failure(
        tool_version_id=tool_name,
        error_code="UNKNOWN_TOOL",
        error_message=f"Unknown tool {tool_name!r}",
    )


def _product_data(product: Any) -> dict[str, Any]:
    """Serialize a product; the stock_insufficient scenario zeroes the stock.

    Shared by every product read so getProductById / substitutes / batch stay
    consistent with getProductByName under the scenario knob.
    """
    stock = 0 if get_scenario() == "stock_insufficient" else product.quantity_in_stock
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        "price": product.price,
        "stock": stock,
        "unit": product.unit,
    }


def _get_product(arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str):
        return ToolResult.failure(
            tool_version_id="getProductByName",
            error_code="INVALID_ARGUMENT",
            error_message=f"name 必须为字符串，got {name!r}",
        )
    product = PRODUCT_BY_NAME.get(name)
    if product is None:
        return ToolResult.failure(
            tool_version_id="getProductByName",
            error_code="PRODUCT_NOT_FOUND",
            error_message=f"Product '{name}' not found",
        )
    return ToolResult.success(
        tool_version_id="getProductByName",
        data=_product_data(product),
    )


def _get_product_by_id(arguments: dict[str, Any]) -> ToolResult:
    product_id = arguments.get("product_id")
    if not isinstance(product_id, int):
        return ToolResult.failure(
            tool_version_id="getProductById",
            error_code="INVALID_ARGUMENT",
            error_message=f"product_id 必须为整数，got {product_id!r}",
        )
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        return ToolResult.failure(
            tool_version_id="getProductById",
            error_code="PRODUCT_NOT_FOUND",
            error_message=f"Product with id {product_id!r} not found",
        )
    return ToolResult.success(tool_version_id="getProductById", data=_product_data(product))


def _substitutes_data(products: list[Any]) -> dict[str, Any]:
    """Serialize substitute matches; an empty list is a legal business answer."""
    return {"substitutes": [_product_data(p) for p in products]}


def _get_product_substitutes(arguments: dict[str, Any]) -> ToolResult:
    product_id = arguments.get("product_id")
    if not isinstance(product_id, int):
        return ToolResult.failure(
            tool_version_id="getProductSubstitutes",
            error_code="INVALID_ARGUMENT",
            error_message=f"product_id 必须为整数，got {product_id!r}",
        )
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        return ToolResult.failure(
            tool_version_id="getProductSubstitutes",
            error_code="PRODUCT_NOT_FOUND",
            error_message=f"Product with id {product_id!r} not found",
        )
    return ToolResult.success(
        tool_version_id="getProductSubstitutes",
        data=_substitutes_data(get_substitutes(product)),
    )


def _get_product_substitutes_by_name(arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str):
        return ToolResult.failure(
            tool_version_id="getProductSubstitutesByName",
            error_code="INVALID_ARGUMENT",
            error_message=f"name 必须为字符串，got {name!r}",
        )
    product = PRODUCT_BY_NAME.get(name)
    if product is None:
        return ToolResult.failure(
            tool_version_id="getProductSubstitutesByName",
            error_code="PRODUCT_NOT_FOUND",
            error_message=f"Product '{name}' not found",
        )
    return ToolResult.success(
        tool_version_id="getProductSubstitutesByName",
        data=_substitutes_data(get_substitutes(product)),
    )


def _get_products_batch(arguments: dict[str, Any]) -> ToolResult:
    start_id = arguments.get("start_id")
    end_id = arguments.get("end_id")
    if not isinstance(start_id, int) or not isinstance(end_id, int):
        return ToolResult.failure(
            tool_version_id="getBatchProductByProductIds",
            error_code="INVALID_ARGUMENT",
            error_message=f"start_id/end_id 必须为整数，got {start_id!r}/{end_id!r}",
        )
    products = get_products_by_id_range(start_id, end_id)
    return ToolResult.success(
        tool_version_id="getBatchProductByProductIds",
        data={"products": [_product_data(p) for p in products]},
    )


def _all_suppliers() -> list[Any]:
    """The live supplier catalog in id order (the index, not the seed list).

    Sourcing reads from the index keeps a runtime-added supplier visible to
    status/region queries — the same reason batch product reads use the index.
    """
    return sorted(SUPPLIER_BY_ID.values(), key=lambda s: s.supplier_id)


def _get_suppliers(arguments: dict[str, Any]) -> ToolResult:
    status = arguments.get("status", "AVAILABLE")
    if get_scenario() == "supplier_unavailable":
        matches = [s for s in _all_suppliers() if s.status != status]
    else:
        matches = [s for s in _all_suppliers() if s.status == status]
    return ToolResult.success(tool_version_id="getSupplierByStatus", data=_suppliers_data(matches))


def _query_suppliers_by_region(arguments: dict[str, Any]) -> ToolResult:
    region = arguments.get("region")
    matches = [s for s in _all_suppliers() if region in s.regions]
    return ToolResult.success(
        tool_version_id="querySuppliersByDeliveryRegion",
        data=_suppliers_data(matches),
    )


def _supplier_data(supplier: Any) -> dict[str, Any]:
    """Serialize a single supplier flat (mirrors the per-item shape in
    ``_suppliers_data``, but without the ``{"suppliers": [...]}`` envelope) so a
    single-supplier lookup reads like getProductById reads a product."""
    return {
        "supplier_id": supplier.supplier_id,
        "name": supplier.name,
        "regions": list(supplier.regions),
        "status": supplier.status,
        "rating": supplier.rating,
        "delivery_days": supplier.delivery_days,
        "price_per_kg": supplier.price_per_kg,
    }


def _get_supplier_by_name(arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str):
        return ToolResult.failure(
            tool_version_id="getSupplierByName",
            error_code="INVALID_ARGUMENT",
            error_message=f"name 必须为字符串，got {name!r}",
        )
    supplier = SUPPLIER_BY_NAME.get(name)
    if supplier is None:
        return ToolResult.failure(
            tool_version_id="getSupplierByName",
            error_code="SUPPLIER_NOT_FOUND",
            error_message=f"Supplier '{name}' not found",
        )
    return ToolResult.success(tool_version_id="getSupplierByName", data=_supplier_data(supplier))


def _get_supplier_by_id(arguments: dict[str, Any]) -> ToolResult:
    supplier_id = arguments.get("supplier_id")
    if not isinstance(supplier_id, int):
        return ToolResult.failure(
            tool_version_id="getSupplierById",
            error_code="INVALID_ARGUMENT",
            error_message=f"supplier_id 必须为整数，got {supplier_id!r}",
        )
    supplier = SUPPLIER_BY_ID.get(supplier_id)
    if supplier is None:
        return ToolResult.failure(
            tool_version_id="getSupplierById",
            error_code="SUPPLIER_NOT_FOUND",
            error_message=f"Supplier with id {supplier_id!r} not found",
        )
    return ToolResult.success(tool_version_id="getSupplierById", data=_supplier_data(supplier))


def _suppliers_data(matches: list[Any]) -> dict[str, Any]:
    """Serialize supplier matches; supplier_id names the first (deterministic)
    one so the create DAG's argument_sources={"supplier_id": "step:s2"} resolves."""
    data: dict[str, Any] = {
        "suppliers": [
            {
                "supplier_id": s.supplier_id,
                "name": s.name,
                "regions": list(s.regions),
                "status": s.status,
                "rating": s.rating,
                "delivery_days": s.delivery_days,
                "price_per_kg": s.price_per_kg,
            }
            for s in matches
        ]
    }
    if matches:
        data["supplier_id"] = matches[0].supplier_id
    return data


def _create_order(arguments: dict[str, Any]) -> ToolResult:
    """Create an order at-most-once per idempotency_key.

    An existing record for the key is replayed (no stock deduction) so a retried
    create never double-applies. Mirrors the simulator's POST /orders route.
    """
    idempotency_key = arguments.get("idempotency_key")
    if not idempotency_key:
        return ToolResult.failure(
            tool_version_id="createOrder",
            error_code="IDEMPOTENCY_KEY_REQUIRED",
            error_message="写操作必须携带 idempotency_key（at-most-once 前提）",
        )

    existing = get_by_idempotency_key(idempotency_key)
    if existing is not None:
        return ToolResult.success(tool_version_id="createOrder", data=_order_to_dict(existing))

    product_id = arguments.get("product_id")
    if not isinstance(product_id, int):
        return ToolResult.failure(
            tool_version_id="createOrder",
            error_code="INVALID_ARGUMENT",
            error_message=f"product_id 必须为整数，got {product_id!r}",
        )
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        return ToolResult.failure(
            tool_version_id="createOrder",
            error_code="PRODUCT_NOT_FOUND",
            error_message=f"Product with id {arguments.get('product_id')!r} not found",
        )

    quantity = arguments.get("quantity")
    if not isinstance(quantity, int) or quantity <= 0:
        return ToolResult.failure(
            tool_version_id="createOrder",
            error_code="INVALID_ARGUMENT",
            error_message=f"quantity 必须为正整数，got {quantity!r}",
        )

    effective_stock = 0 if get_scenario() == "stock_insufficient" else product.quantity_in_stock
    if quantity > effective_stock:
        return ToolResult.failure(
            tool_version_id="createOrder",
            error_code="INSUFFICIENT_STOCK",
            error_message=(
                f"Insufficient stock: requested {quantity} but only {effective_stock} available"
            ),
        )

    supplier_id = arguments.get("supplier_id")
    region = arguments.get("region")
    if not isinstance(supplier_id, int) or not isinstance(region, str):
        return ToolResult.failure(
            tool_version_id="createOrder",
            error_code="INVALID_ARGUMENT",
            error_message="supplier_id 必须为整数且 region 必须为字符串",
        )

    product.quantity_in_stock -= quantity
    order = create_order(
        product_id=product.product_id,
        product_name=product.name,
        quantity=quantity,
        supplier_id=supplier_id,
        region=region,
        unit_price=product.price,
        idempotency_key=idempotency_key,
    )
    return ToolResult.success(tool_version_id="createOrder", data=_order_to_dict(order))


def _update_order_status(arguments: dict[str, Any]) -> ToolResult:
    """Apply a status transition at-most-once per idempotency_key.

    Mirrors the simulator's PUT /orders/updateOrderStatus route. Illegal
    transitions (terminal state, rollback, skip-level) are permanent failures —
    the status state machine is deterministic, so a retry cannot succeed.
    """
    idempotency_key = arguments.get("idempotency_key")
    if not idempotency_key:
        return ToolResult.failure(
            tool_version_id="updateOrderStatus",
            error_code="IDEMPOTENCY_KEY_REQUIRED",
            error_message="写操作必须携带 idempotency_key（at-most-once 前提）",
        )

    order_id = arguments.get("order_id")
    if not isinstance(order_id, str):
        return ToolResult.failure(
            tool_version_id="updateOrderStatus",
            error_code="INVALID_ARGUMENT",
            error_message=f"order_id 必须为字符串，got {order_id!r}",
        )
    status = arguments.get("status")
    if not isinstance(status, str):
        return ToolResult.failure(
            tool_version_id="updateOrderStatus",
            error_code="INVALID_ARGUMENT",
            error_message=f"status 必须为字符串，got {status!r}",
        )

    try:
        order = update_order_status(order_id, status, idempotency_key)
    except ValueError as exc:
        return ToolResult.failure(
            tool_version_id="updateOrderStatus",
            error_code="INVALID_STATUS_TRANSITION",
            error_message=str(exc),
        )
    if order is None:
        return ToolResult.failure(
            tool_version_id="updateOrderStatus",
            error_code="ORDER_NOT_FOUND",
            error_message=f"Order '{order_id}' not found",
        )
    return ToolResult.success(tool_version_id="updateOrderStatus", data=_order_to_dict(order))


def _cancel_order(arguments: dict[str, Any]) -> ToolResult:
    """Cancel an order at-most-once per idempotency_key.

    Mirrors the simulator's DELETE /orders/cancelOrder route. Only
    CREATED/CONFIRMED/SHIPPED orders may reach CANCELLED; a terminal order is a
    permanent failure.
    """
    idempotency_key = arguments.get("idempotency_key")
    if not idempotency_key:
        return ToolResult.failure(
            tool_version_id="cancelOrder",
            error_code="IDEMPOTENCY_KEY_REQUIRED",
            error_message="写操作必须携带 idempotency_key（at-most-once 前提）",
        )

    order_id = arguments.get("order_id")
    if not isinstance(order_id, str):
        return ToolResult.failure(
            tool_version_id="cancelOrder",
            error_code="INVALID_ARGUMENT",
            error_message=f"order_id 必须为字符串，got {order_id!r}",
        )

    try:
        order = cancel_order(order_id, idempotency_key)
    except ValueError as exc:
        return ToolResult.failure(
            tool_version_id="cancelOrder",
            error_code="INVALID_STATUS_TRANSITION",
            error_message=str(exc),
        )
    if order is None:
        return ToolResult.failure(
            tool_version_id="cancelOrder",
            error_code="ORDER_NOT_FOUND",
            error_message=f"Order '{order_id}' not found",
        )
    return ToolResult.success(tool_version_id="cancelOrder", data=_order_to_dict(order))


def _get_order(arguments: dict[str, Any]) -> ToolResult:
    order_id = arguments.get("order_id")
    if not isinstance(order_id, str):
        return ToolResult.failure(
            tool_version_id="getOrderByOrderId",
            error_code="INVALID_ARGUMENT",
            error_message=f"order_id 必须为字符串，got {order_id!r}",
        )
    order = get_by_id(order_id)
    if order is None:
        return ToolResult.failure(
            tool_version_id="getOrderByOrderId",
            error_code="ORDER_NOT_FOUND",
            error_message=f"Order '{order_id}' not found",
        )
    return ToolResult.success(tool_version_id="getOrderByOrderId", data=_order_to_dict(order))


def _orders_data(orders: list[Any]) -> dict[str, Any]:
    """Serialize order matches; an empty list is a legal business answer."""
    return {"orders": [_order_to_dict(o) for o in orders]}


def _get_orders_by_supplier(arguments: dict[str, Any]) -> ToolResult:
    supplier_id = arguments.get("supplier_id")
    if not isinstance(supplier_id, int):
        return ToolResult.failure(
            tool_version_id="getOrdersBySupplierId",
            error_code="INVALID_ARGUMENT",
            error_message=f"supplier_id 必须为整数，got {supplier_id!r}",
        )
    return ToolResult.success(
        tool_version_id="getOrdersBySupplierId",
        data=_orders_data(get_orders_by_supplier(supplier_id)),
    )


def _get_orders_by_product(arguments: dict[str, Any]) -> ToolResult:
    product_id = arguments.get("product_id")
    if not isinstance(product_id, int):
        return ToolResult.failure(
            tool_version_id="getByProductId",
            error_code="INVALID_ARGUMENT",
            error_message=f"product_id 必须为整数，got {product_id!r}",
        )
    return ToolResult.success(
        tool_version_id="getByProductId",
        data=_orders_data(get_orders_by_product(product_id)),
    )


def _get_orders_by_status(arguments: dict[str, Any]) -> ToolResult:
    status = arguments.get("status")
    if not isinstance(status, str):
        return ToolResult.failure(
            tool_version_id="getByOrderStatus",
            error_code="INVALID_ARGUMENT",
            error_message=f"status 必须为字符串，got {status!r}",
        )
    return ToolResult.success(
        tool_version_id="getByOrderStatus",
        data=_orders_data(get_orders_by_status(status)),
    )


def _get_orders_by_time_range(arguments: dict[str, Any]) -> ToolResult:
    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        return ToolResult.failure(
            tool_version_id="getByTimeRange",
            error_code="INVALID_ARGUMENT",
            error_message=f"start_date/end_date 必须为字符串，got {start_date!r}/{end_date!r}",
        )
    return ToolResult.success(
        tool_version_id="getByTimeRange",
        data=_orders_data(get_orders_by_time_range(start_date, end_date)),
    )


def _order_to_dict(order: Any) -> dict[str, Any]:
    """Serialize an order in the same shape as the simulator's orders route."""
    return {
        "order_id": order.order_id,
        "product_id": order.product_id,
        "product_name": order.product_name,
        "quantity": order.quantity,
        "supplier_id": order.supplier_id,
        "region": order.region,
        "amount": order.amount,
        "status": order.status,
        "idempotency_key": order.idempotency_key,
        "created_at": order.created_at,
    }


# -- Cloud ERP HTTP executor (V5 calling convention) -------------------------

_ERP_HTTP_TIMEOUT = 15.0  # seconds; the graph's step timeouts still apply on top.

# The cloud API's supplier status enum; V6 uses AVAILABLE / UNAVAILABLE.
_ERP_STATUS_ACTIVE = "InUse"
_ERP_STATUS_DISABLED = "DisUse"
_V6_STATUS_ACTIVE = "AVAILABLE"
_V6_STATUS_DISABLED = "UNAVAILABLE"


def _map_supplier_status(cloud_status: str | None) -> str:
    return _V6_STATUS_ACTIVE if cloud_status == _ERP_STATUS_ACTIVE else _V6_STATUS_DISABLED


def _normalize_product(payload: dict[str, Any]) -> dict[str, Any]:
    """Cloud Product (camelCase) -> the simulator executor's product shape.

    ``unit`` is not returned by the cloud API and nothing downstream consumes
    it (the deterministic planner's getProductByName success_condition only
    reads ``name``), so it is omitted.
    """
    return {
        "product_id": payload["productId"],
        "name": payload["name"],
        "description": payload["description"],
        "price": payload["price"],
        "stock": payload["quantityInStock"],
    }


def _normalize_substitutes(payload: Any) -> dict[str, Any]:
    """Cloud substitute result -> the executor's {"substitutes": [...]} shape.

    The OpenAPI schema models a single Product but the live API may answer a
    bare list; both are tolerated. An empty list is a legal "no substitute"
    business answer.
    """
    items = payload if isinstance(payload, list) else [payload]
    return {"substitutes": [_normalize_product(item) for item in items if isinstance(item, dict)]}


def _normalize_product_list(payload: Any) -> dict[str, Any]:
    """Cloud batch result (bare array of Products) -> {"products": [...]}."""
    items = payload if isinstance(payload, list) else []
    return {"products": [_normalize_product(item) for item in items if isinstance(item, dict)]}


def _normalize_supplier(payload: dict[str, Any]) -> dict[str, Any]:
    # The cloud's deliveryAreas is an array of {region, ...} per the OpenAPI
    # spec but the live API returns plain region strings ("北京", "上海"). V6
    # uses a plain list of region strings either way, so both item forms are
    # tolerated. delivery_days / price_per_kg have no cloud field.
    delivery_areas = payload.get("deliveryAreas") or []
    regions = [area if isinstance(area, str) else area["region"] for area in delivery_areas]
    return {
        "supplier_id": payload["supplierId"],
        "name": payload["name"],
        "regions": regions,
        "status": _map_supplier_status(payload.get("status")),
        "rating": payload.get("rating", 0.0),
    }


def _normalize_suppliers(payload: Any) -> dict[str, Any]:
    """Normalize a supplier query result; tolerates a bare list or a single dict.

    The top-level ``supplier_id`` names the first match so the create DAG's
    argument_sources={"supplier_id": "step:s2"} resolves, mirroring
    ``_suppliers_data`` for the in-process simulator.
    """
    items = payload if isinstance(payload, list) else [payload]
    suppliers = [_normalize_supplier(item) for item in items if isinstance(item, dict)]
    data: dict[str, Any] = {"suppliers": suppliers}
    if suppliers:
        data["supplier_id"] = suppliers[0]["supplier_id"]
    return data


def _normalize_order(payload: dict[str, Any]) -> dict[str, Any]:
    """Cloud Order (camelCase) -> the simulator executor's order shape.

    The cloud order carries no product_name and no idempotency_key — both are
    omitted (nothing downstream reads them off a cloud result).
    """
    return {
        "order_id": payload["id"],
        "product_id": payload["productId"],
        "quantity": payload["quantity"],
        "supplier_id": payload["supplierId"],
        "region": payload["orderRegion"],
        "amount": payload["amount"],
        "status": payload["status"],
        "created_at": payload["orderTime"],
    }


def _normalize_orders(payload: Any) -> dict[str, Any]:
    """Cloud order-query result (bare array of Orders) -> {"orders": [...]}.

    The cloud order carries no product_name and no idempotency_key — both are
    omitted, mirroring ``_normalize_order``. An empty array is a legal business
    answer ("no orders match this query"), not an error.
    """
    items = payload if isinstance(payload, list) else []
    return {"orders": [_normalize_order(item) for item in items if isinstance(item, dict)]}


def _permanent_failure(tool_version_id: str, error_code: str, error_message: str) -> ToolResult:
    return ToolResult.failure(
        tool_version_id=tool_version_id,
        error_code=error_code,
        error_message=error_message,
        is_retryable=False,
    )


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    response = await client.request(method, path, params=params, json=json, headers=headers)
    # A redirect is the cloud's "invalid request / not found" fallback (302 ->
    # /login, Spring Security style); raise before the HTML page hits json() so
    # a tool branch can decide, and anything unhandled maps to UPSTREAM_{code}.
    if response.is_redirect:
        raise httpx.HTTPStatusError(
            f"Cloud ERP returned redirect {response.status_code} for {response.request.url}",
            request=response.request,
            response=response,
        )
    response.raise_for_status()
    return response.json()


async def _dispatch_http(
    client: httpx.AsyncClient,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
) -> ToolResult:
    """Route one tool call to the cloud ERP and normalize its response.

    Only the 5 tools the in-process simulator executor maps are supported; the
    rest fail closed with UNKNOWN_TOOL (same as the simulator path).
    """
    if tool_name == "getProductByName":
        payload = await _request_json(
            client,
            "POST",
            "/products/getProductByName",
            headers=headers,
            json={"name": arguments["name"]},
        )
        if payload.get("productId") is None:
            return _permanent_failure(
                tool_name, "PRODUCT_NOT_FOUND", f"Product '{arguments.get('name')!r}' not found"
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_product(payload))

    if tool_name == "getProductById":
        payload = await _request_json(
            client,
            "POST",
            "/products/getProductById",
            headers=headers,
            json={"productId": arguments["product_id"]},
        )
        if payload.get("productId") is None:
            return _permanent_failure(
                tool_name,
                "PRODUCT_NOT_FOUND",
                f"Product with id {arguments.get('product_id')!r} not found",
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_product(payload))

    if tool_name == "getProductSubstitutes":
        payload = await _request_json(
            client,
            "POST",
            "/products/getProductSubstitutes",
            headers=headers,
            json={"productId": arguments["product_id"]},
        )
        # A bare list is the substitute set (possibly empty) — the cloud cannot
        # signal "product not found" in that shape. A dict with no productId is
        # the unknown-product answer.
        if isinstance(payload, list):
            return ToolResult.success(
                tool_version_id=tool_name, data=_normalize_substitutes(payload)
            )
        if payload.get("productId") is None:
            return _permanent_failure(
                tool_name,
                "PRODUCT_NOT_FOUND",
                f"Product with id {arguments.get('product_id')!r} not found",
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_substitutes(payload))

    if tool_name == "getProductSubstitutesByName":
        payload = await _request_json(
            client,
            "POST",
            "/products/getProductSubstitutesByName",
            headers=headers,
            json={"name": arguments["name"]},
        )
        if isinstance(payload, list):
            return ToolResult.success(
                tool_version_id=tool_name, data=_normalize_substitutes(payload)
            )
        if payload.get("productId") is None:
            return _permanent_failure(
                tool_name, "PRODUCT_NOT_FOUND", f"Product '{arguments.get('name')!r}' not found"
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_substitutes(payload))

    if tool_name == "getBatchProductByProductIds":
        payload = await _request_json(
            client,
            "POST",
            "/products/getBatchProductByProductIds",
            headers=headers,
            json={"startId": arguments["start_id"], "endId": arguments["end_id"]},
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_product_list(payload))

    if tool_name == "getSupplierByStatus":
        cloud_status = (
            _ERP_STATUS_ACTIVE
            if arguments.get("status", _V6_STATUS_ACTIVE) == _V6_STATUS_ACTIVE
            else _ERP_STATUS_DISABLED
        )
        payload = await _request_json(
            client,
            "GET",
            "/suppliers/getSupplierByStatus",
            headers=headers,
            params={"status": cloud_status},
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_suppliers(payload))

    if tool_name == "querySuppliersByDeliveryRegion":
        payload = await _request_json(
            client,
            "POST",
            "/suppliers/querySuppliersByDeliveryRegion",
            headers=headers,
            json={"region": arguments["region"]},
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_suppliers(payload))

    if tool_name == "getSupplierByName":
        payload = await _request_json(
            client,
            "GET",
            "/suppliers/getSupplierByName",
            headers=headers,
            params={"supplierName": arguments["name"]},
        )
        if payload.get("supplierId") is None:
            return _permanent_failure(
                tool_name,
                "SUPPLIER_NOT_FOUND",
                f"Supplier '{arguments.get('name')!r}' not found",
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_supplier(payload))

    if tool_name == "getSupplierById":
        payload = await _request_json(
            client,
            "GET",
            f"/suppliers/getSupplierById/{arguments['supplier_id']}",
            headers=headers,
        )
        if payload.get("supplierId") is None:
            return _permanent_failure(
                tool_name,
                "SUPPLIER_NOT_FOUND",
                f"Supplier with id {arguments.get('supplier_id')!r} not found",
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_supplier(payload))

    if tool_name == "createOrder":
        # idempotency_key is deliberately dropped: the cloud createOrder API
        # has no such field, so at-most-once rests solely on the DB
        # IdempotencyStore replay — a lost-response retry can duplicate a cloud
        # order (honest limitation, unlike the in-process simulator).
        payload = await _request_json(
            client,
            "POST",
            "/orders/createOrder",
            headers=headers,
            json={
                "quantity": arguments["quantity"],
                "productId": arguments["product_id"],
                "supplierId": arguments["supplier_id"],
                "orderRegion": arguments["region"],
            },
        )
        if payload.get("id") in (None, -1):
            # id == -1 is the cloud's business-failure sentinel: a valid request
            # that fails business rules returns 200 + {"id": -1, "status": <中文
            # 原因>} instead of a 4xx. Normalizing it as an order would fabricate
            # an order that never exists.
            reason = payload.get("status") or "cloud returned no order"
            return _permanent_failure(
                tool_name, "ORDER_CREATE_FAILED", f"Cloud ERP rejected order: {reason}"
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_order(payload))

    if tool_name == "updateOrderStatus":
        # idempotency_key is deliberately dropped: the cloud updateOrderStatus
        # API has no idempotency field, so at-most-once rests solely on the DB
        # IdempotencyStore replay (honest limitation, same as createOrder). The
        # newStatus is sent as the V6 enum verbatim — 待对真实 API 确认.
        payload = await _request_json(
            client,
            "PUT",
            "/orders/updateOrderStatus",
            headers=headers,
            json={"orderId": arguments["order_id"], "newStatus": arguments["status"]},
        )
        if payload.get("id") in (None, -1):
            reason = payload.get("status") or "cloud returned no order"
            return _permanent_failure(
                tool_name,
                "ORDER_UPDATE_FAILED",
                f"Cloud ERP rejected order update: {reason}",
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_order(payload))

    if tool_name == "cancelOrder":
        # idempotency_key deliberately dropped — same honest limitation as
        # updateOrderStatus above.
        payload = await _request_json(
            client,
            "DELETE",
            "/orders/cancelOrder",
            headers=headers,
            json={"orderId": arguments["order_id"]},
        )
        if payload.get("id") in (None, -1):
            reason = payload.get("status") or "cloud returned no order"
            return _permanent_failure(
                tool_name,
                "ORDER_CANCEL_FAILED",
                f"Cloud ERP rejected order cancel: {reason}",
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_order(payload))

    if tool_name == "getOrderByOrderId":
        try:
            payload = await _request_json(
                client,
                "POST",
                "/orders/getOrderByOrderId",
                headers=headers,
                json={"orderId": arguments["order_id"]},
            )
        except httpx.HTTPStatusError as exc:
            # The cloud's "not found" fallback for a missing order is a 302 to
            # /login (Spring Security style), not a 404. Intercept it before the
            # generic UPSTREAM_{code} mapping so the read-back leg reports
            # ORDER_NOT_FOUND, matching the in-process simulator.
            if exc.response.status_code == 302:
                return _permanent_failure(
                    tool_name,
                    "ORDER_NOT_FOUND",
                    f"Order '{arguments.get('order_id')!r}' not found (cloud returned 302)",
                )
            raise
        # The cloud wraps the order in {"order": {...}} with a parallel
        # {"supplier": {...}} object — not the flat order shape createOrder
        # returns. Unwrap the envelope before checking the id; a bare payload
        # (empty or flat) is treated as the order itself.
        order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        if order.get("id") is None:
            return _permanent_failure(
                tool_name, "ORDER_NOT_FOUND", f"Order '{arguments.get('order_id')!r}' not found"
            )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_order(order))

    if tool_name == "getOrdersBySupplierId":
        payload = await _request_json(
            client,
            "POST",
            "/orders/getOrdersBySupplierId",
            headers=headers,
            json={"supplierId": arguments["supplier_id"]},
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_orders(payload))

    if tool_name == "getByProductId":
        payload = await _request_json(
            client,
            "POST",
            "/orders/getByProductId",
            headers=headers,
            json={"productId": arguments["product_id"]},
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_orders(payload))

    if tool_name == "getByOrderStatus":
        payload = await _request_json(
            client,
            "POST",
            "/orders/getByOrderStatus",
            headers=headers,
            json={"status": arguments["status"]},
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_orders(payload))

    if tool_name == "getByTimeRange":
        payload = await _request_json(
            client,
            "POST",
            "/orders/getByTimeRange",
            headers=headers,
            json={
                "startDate": arguments["start_date"],
                "endDate": arguments["end_date"],
            },
        )
        return ToolResult.success(tool_version_id=tool_name, data=_normalize_orders(payload))

    return ToolResult.failure(
        tool_version_id=tool_name,
        error_code="UNKNOWN_TOOL",
        error_message=f"Unknown tool {tool_name!r}",
    )


def build_erp_http_executor(
    base_url: str,
    api_key: str,
) -> Callable[[str, dict[str, Any]], Awaitable[ToolResult]]:
    """Build the async cloud-ERP executor for the worker's agent graph.

    Mirrors ``erp_simulator_executor``'s contract: every tool returns a
    ToolResult (success or failure) — the verify node requires a StepResult per
    step, so nothing here may raise. The outbound URL is the operator-configured
    base URL plus a fixed constant path (never prompt-derived), so there is no
    SSRF surface. Error mapping: network/timeout and HTTP >= 500 are retryable
    (drive AsyncRetryExecutor); 4xx and malformed responses are permanent.
    """
    headers = {"X-API-Key": api_key}

    async def erp_http_executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=_ERP_HTTP_TIMEOUT) as client:
                return await _dispatch_http(client, tool_name, arguments, headers)
        except httpx.TimeoutException as exc:
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="TIMEOUT",
                error_message=f"Cloud ERP request timed out: {exc}",
                is_retryable=True,
            )
        except httpx.TransportError as exc:
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="UPSTREAM_UNAVAILABLE",
                error_message=f"Cloud ERP unreachable: {exc}",
                is_retryable=True,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code=f"UPSTREAM_{code}",
                error_message=f"Cloud ERP returned HTTP {code}",
                is_retryable=code >= 500,
            )
        except (KeyError, TypeError, ValueError, httpx.InvalidURL) as exc:
            # Non-JSON body, a response shape that does not match the cloud
            # contract, or a malformed base_url — normalizing is pointless, so
            # fail permanently (the executor must never raise).
            return _permanent_failure(
                tool_name,
                "INVALID_RESPONSE",
                f"Cloud ERP response could not be parsed: {exc}",
            )

    return erp_http_executor


def resolve_erp_executor(
    settings: Settings,
) -> Callable[[str, dict[str, Any]], Awaitable[ToolResult]]:
    """Pick the worker executor from settings: MCP, then cloud ERP, else simulator.

    Config-driven with simulator fallback, newest path first: setting
    MCP_SERVER_URL activates the real-transport MCP executor; otherwise
    ERP_API_BASE_URL activates the HTTP cloud executor; leaving both empty keeps
    the in-process deterministic simulator, so offline and test environments
    make no network calls.
    """
    if settings.mcp_server_url:
        return build_mcp_executor(settings.mcp_server_url)
    if settings.erp_api_base_url:
        return build_erp_http_executor(
            settings.erp_api_base_url,
            settings.erp_api_key.get_secret_value(),
        )
    return erp_simulator_executor
