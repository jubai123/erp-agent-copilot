"""Demo MCP tool server — proves real end-to-end MCP connectivity.

Retires the "MCP 是展示件" critique: a client built on
:class:`MCPGatewayConnection` can initialize, list tools, and execute them
against this server over a real Streamable HTTP transport with no mocks. The
server exposes the simulator tool contract the worker's executor maps (the
product reads getProductByName/getProductById/getProductSubstitutes/
getProductSubstitutesByName/getBatchProductByProductIds, the supplier reads,
createOrder, getOrderByOrderId), backed by the same seed data
(apps.erp_simulator.data), so the tool contract is the one the agent already
validates against (docs/05). createOrder writes into the simulator's in-memory
store (delegating to apps.erp_simulator.data.orders), deducts stock, and
replays the existing order for the same idempotency_key — the at-most-once
behaviour the write DAGs rely on.

A business failure (unknown product, insufficient stock, missing
idempotency_key) raises ValueError, which the SDK surfaces as a
CallToolResult(is_error=True) — the MCP analogue of ToolResult.failure.

Run:  python -m apps.mcp_gateway.demo_server
Then connect a client to:  http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from apps.erp_simulator.data.orders import create_order, get_by_id, get_by_idempotency_key
from apps.erp_simulator.data.products import (
    PRODUCT_BY_ID,
    PRODUCT_BY_NAME,
    get_products_by_id_range,
    get_substitutes,
)
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS

HOST = "127.0.0.1"
PORT = 8765
MCP_PATH = "/mcp"


def _product_dict(product: Any) -> dict[str, Any]:
    """Serialize a product in the same shape as the simulator executor."""
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        "price": product.price,
        "stock": product.quantity_in_stock,
        "unit": product.unit,
    }


def _suppliers_data(matches: list[Any]) -> dict[str, Any]:
    """Serialize supplier matches; supplier_id names the first (deterministic)
    one so the create DAG's argument_sources={"supplier_id": "step:s2"} resolves.
    """
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


def _order_to_dict(order: Any) -> dict[str, Any]:
    """Serialize an order in the same shape as the simulator executor."""
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


def build_demo_server() -> MCPServer:
    """Build an MCPServer exposing the simulator's five tools over MCP."""
    server = MCPServer(
        name="erp-demo",
        version="0.1.0",
        instructions=(
            "Demo ERP tools over MCP. Reads and writes the same in-process "
            "catalog as the ERP Simulator; nothing is persisted."
        ),
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "erp-demo-mcp"})

    @server.tool(name="getProductByName")
    def get_product_by_name(name: str) -> dict[str, Any]:
        """Return a product's catalog fields by name (mirrors the simulator executor).

        Raises ValueError when the product is unknown, which the SDK surfaces as a
        CallToolResult with is_error=True — the MCP analogue of ToolResult.failure.
        """
        product = PRODUCT_BY_NAME.get(name)
        if product is None:
            raise ValueError(f"Product '{name}' not found")
        return _product_dict(product)

    @server.tool(name="getProductById")
    def get_product_by_id(product_id: int) -> dict[str, Any]:
        """Return a product's catalog fields by id (mirrors the simulator executor)."""
        product = PRODUCT_BY_ID.get(product_id)
        if product is None:
            raise ValueError(f"Product with id {product_id!r} not found")
        return _product_dict(product)

    @server.tool(name="getProductSubstitutes")
    def get_product_substitutes(product_id: int) -> dict[str, Any]:
        """Return a product's substitutes by id; an empty list is a legal answer."""
        product = PRODUCT_BY_ID.get(product_id)
        if product is None:
            raise ValueError(f"Product with id {product_id!r} not found")
        return {"substitutes": [_product_dict(p) for p in get_substitutes(product)]}

    @server.tool(name="getProductSubstitutesByName")
    def get_product_substitutes_by_name(name: str) -> dict[str, Any]:
        """Return a product's substitutes by name; an empty list is a legal answer."""
        product = PRODUCT_BY_NAME.get(name)
        if product is None:
            raise ValueError(f"Product '{name}' not found")
        return {"substitutes": [_product_dict(p) for p in get_substitutes(product)]}

    @server.tool(name="getBatchProductByProductIds")
    def get_batch_product_by_product_ids(start_id: int, end_id: int) -> dict[str, Any]:
        """Return products whose id falls in [start_id, end_id], in id order."""
        return {
            "products": [_product_dict(p) for p in get_products_by_id_range(start_id, end_id)]
        }

    @server.tool(name="getSupplierByStatus")
    def get_supplier_by_status(status: str = "AVAILABLE") -> dict[str, Any]:
        """Return suppliers with *status* (default AVAILABLE), first as supplier_id."""
        return _suppliers_data([s for s in SEED_SUPPLIERS if s.status == status])

    @server.tool(name="querySuppliersByDeliveryRegion")
    def query_suppliers_by_delivery_region(region: str) -> dict[str, Any]:
        """Return suppliers covering *region*, with the first as ``supplier_id``."""
        return _suppliers_data([s for s in SEED_SUPPLIERS if region in s.regions])

    @server.tool(name="createOrder")
    def create_order_tool(
        idempotency_key: str,
        product_id: int,
        quantity: int,
        supplier_id: int,
        region: str,
    ) -> dict[str, Any]:
        """Create an order at-most-once per idempotency_key (mirrors the executor).

        An existing record for the key is replayed (no stock deduction) so a
        retried create never double-applies; a business violation raises
        ValueError, surfaced as is_error=True.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key is required (at-most-once premise)")
        existing = get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return _order_to_dict(existing)

        product = PRODUCT_BY_ID.get(product_id)
        if product is None:
            raise ValueError(f"Product with id {product_id!r} not found")
        if quantity <= 0:
            raise ValueError(f"quantity must be a positive integer, got {quantity!r}")
        if quantity > product.quantity_in_stock:
            raise ValueError(
                f"Insufficient stock: requested {quantity} but only "
                f"{product.quantity_in_stock} available"
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
        return _order_to_dict(order)

    @server.tool(name="getOrderByOrderId")
    def get_order_by_order_id(order_id: str) -> dict[str, Any]:
        """Return an order by its order_id; raises ValueError when unknown."""
        order = get_by_id(order_id)
        if order is None:
            raise ValueError(f"Order '{order_id}' not found")
        return _order_to_dict(order)

    return server


def build_demo_app():
    """Return the Starlette ASGI app that serves the demo MCP server over HTTP."""
    return build_demo_server().streamable_http_app(
        streamable_http_path=MCP_PATH,
        host=HOST,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_demo_app(), host=HOST, port=PORT)
