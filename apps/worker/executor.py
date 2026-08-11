"""Deterministic ERP-simulator tool executor for the worker's agent graph.

Task 4.16: the worker drives the LangGraph, so the tool calls inside
execute_ready_steps resolve here — the same in-process simulator data the old
direct path used (apps.erp_simulator.data.products / .suppliers), now behind
the async (tool_name, arguments) -> ToolResult contract the executor node
expects. The scenario behavior (timeout / stock_insufficient /
supplier_unavailable) is preserved so the e2e scenario tests keep passing.
"""

from __future__ import annotations

from typing import Any

from apps.erp_simulator.data.products import PRODUCT_BY_NAME
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS
from apps.erp_simulator.scenarios import get_scenario
from erp_copilot.tools.tool_result import ToolResult

_TIMEOUT_ERROR = "Request timed out contacting ERP simulator"


async def erp_simulator_executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    """Execute one tool against the in-process ERP simulator data.

    The timeout scenario fails every tool (retryable); product/supplier lookups
    add their own branches. Unknown tools fail closed (UNKNOWN_TOOL).
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
