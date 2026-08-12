"""Deterministic ERP-simulator tool executor for the worker's agent graph.

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
"""

from __future__ import annotations

from typing import Any

from apps.erp_simulator.data.orders import create_order, get_by_id, get_by_idempotency_key
from apps.erp_simulator.data.products import PRODUCT_BY_ID, PRODUCT_BY_NAME
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS
from apps.erp_simulator.scenarios import get_scenario
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
    if tool_name == "getSupplierByStatus":
        return _get_suppliers(arguments)
    if tool_name == "createOrder":
        return _create_order(arguments)
    if tool_name == "getOrderByOrderId":
        return _get_order(arguments)
    return ToolResult.failure(
        tool_version_id=tool_name,
        error_code="UNKNOWN_TOOL",
        error_message=f"Unknown tool {tool_name!r}",
    )


def _get_product(arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    product = PRODUCT_BY_NAME.get(name)
    if product is None:
        return ToolResult.failure(
            tool_version_id="getProductByName",
            error_code="PRODUCT_NOT_FOUND",
            error_message=f"Product '{name}' not found",
        )
    stock = 0 if get_scenario() == "stock_insufficient" else product.quantity_in_stock
    return ToolResult.success(
        tool_version_id="getProductByName",
        data={
            "product_id": product.product_id,
            "name": product.name,
            "description": product.description,
            "price": product.price,
            "stock": stock,
            "unit": product.unit,
        },
    )


def _get_suppliers(arguments: dict[str, Any]) -> ToolResult:
    status = arguments.get("status", "AVAILABLE")
    if get_scenario() == "supplier_unavailable":
        suppliers = [s for s in SEED_SUPPLIERS if s.status != status]
    else:
        suppliers = [s for s in SEED_SUPPLIERS if s.status == status]
    return ToolResult.success(
        tool_version_id="getSupplierByStatus",
        data={
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
                for s in suppliers
            ]
        },
    )


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

    product = PRODUCT_BY_ID.get(arguments.get("product_id"))
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

    product.quantity_in_stock -= quantity
    order = create_order(
        product_id=product.product_id,
        product_name=product.name,
        quantity=quantity,
        supplier_id=arguments.get("supplier_id"),
        region=arguments.get("region"),
        unit_price=product.price,
        idempotency_key=idempotency_key,
    )
    return ToolResult.success(tool_version_id="createOrder", data=_order_to_dict(order))


def _get_order(arguments: dict[str, Any]) -> ToolResult:
    order_id = arguments.get("order_id")
    order = get_by_id(order_id)
    if order is None:
        return ToolResult.failure(
            tool_version_id="getOrderByOrderId",
            error_code="ORDER_NOT_FOUND",
            error_message=f"Order '{order_id}' not found",
        )
    return ToolResult.success(tool_version_id="getOrderByOrderId", data=_order_to_dict(order))


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
