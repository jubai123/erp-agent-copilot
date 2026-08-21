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
    add_product,
    get_products_by_id_range,
    get_substitutes,
    remove_product_by_id,
    remove_product_by_name,
    update_product_description,
    update_product_substitutes,
)
from apps.erp_simulator.data.suppliers import (
    SUPPLIER_BY_ID,
    SUPPLIER_BY_NAME,
    add_supplier,
    delete_supplier_by_id,
    delete_supplier_by_name,
)

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


def _supplier_dict(supplier: Any) -> dict[str, Any]:
    """Serialize a single supplier flat, matching the simulator executor."""
    return {
        "supplier_id": supplier.supplier_id,
        "name": supplier.name,
        "regions": list(supplier.regions),
        "status": supplier.status,
        "rating": supplier.rating,
        "delivery_days": supplier.delivery_days,
        "price_per_kg": supplier.price_per_kg,
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


def _orders_data(orders: list[Any]) -> dict[str, Any]:
    """Serialize order matches; an empty list is a legal business answer."""
    return {"orders": [_order_to_dict(o) for o in orders]}


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
        return {"products": [_product_dict(p) for p in get_products_by_id_range(start_id, end_id)]}

    @server.tool(name="getSupplierByStatus")
    def get_supplier_by_status(status: str = "AVAILABLE") -> dict[str, Any]:
        """Return suppliers with *status* (default AVAILABLE), first as supplier_id."""
        suppliers = sorted(SUPPLIER_BY_ID.values(), key=lambda s: s.supplier_id)
        return _suppliers_data([s for s in suppliers if s.status == status])

    @server.tool(name="querySuppliersByDeliveryRegion")
    def query_suppliers_by_delivery_region(region: str) -> dict[str, Any]:
        """Return suppliers covering *region*, with the first as ``supplier_id``."""
        suppliers = sorted(SUPPLIER_BY_ID.values(), key=lambda s: s.supplier_id)
        return _suppliers_data([s for s in suppliers if region in s.regions])

    @server.tool(name="getSupplierByName")
    def get_supplier_by_name(name: str) -> dict[str, Any]:
        """Return a single supplier's fields by name (mirrors the simulator executor)."""
        supplier = SUPPLIER_BY_NAME.get(name)
        if supplier is None:
            raise ValueError(f"Supplier '{name}' not found")
        return _supplier_dict(supplier)

    @server.tool(name="getSupplierById")
    def get_supplier_by_id(supplier_id: int) -> dict[str, Any]:
        """Return a single supplier's fields by id (mirrors the simulator executor)."""
        supplier = SUPPLIER_BY_ID.get(supplier_id)
        if supplier is None:
            raise ValueError(f"Supplier with id {supplier_id!r} not found")
        return _supplier_dict(supplier)

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

    @server.tool(name="updateOrderStatus")
    def update_order_status_tool(
        order_id: str, status: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Advance an order's status at-most-once per idempotency_key.

        Mirrors the simulator executor: a replay (same key) returns the recorded
        order without re-applying; an illegal transition raises ValueError,
        surfaced as is_error=True.
        """
        order = update_order_status(order_id, status, idempotency_key)
        if order is None:
            raise ValueError(f"Order '{order_id}' not found")
        return _order_to_dict(order)

    @server.tool(name="cancelOrder")
    def cancel_order_tool(order_id: str, idempotency_key: str) -> dict[str, Any]:
        """Cancel an order at-most-once per idempotency_key (mirrors the executor).

        Only CREATED/CONFIRMED/SHIPPED orders may be cancelled; a terminal order
        raises ValueError, surfaced as is_error=True.
        """
        order = cancel_order(order_id, idempotency_key)
        if order is None:
            raise ValueError(f"Order '{order_id}' not found")
        return _order_to_dict(order)

    @server.tool(name="addProduct")
    def add_product_tool(
        idempotency_key: str,
        name: str,
        price: float,
        quantity_in_stock: int,
        description: str = "",
    ) -> dict[str, Any]:
        """Add a product at-most-once per idempotency_key (mirrors the executor)."""
        if not idempotency_key:
            raise ValueError("idempotency_key is required (at-most-once premise)")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError(f"price must be a number, got {price!r}")
        if isinstance(quantity_in_stock, bool) or not isinstance(quantity_in_stock, int):
            raise ValueError(f"quantity_in_stock must be an int, got {quantity_in_stock!r}")
        try:
            product = add_product(
                name=name,
                description=description,
                price=price,
                quantity_in_stock=quantity_in_stock,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return _product_dict(product)

    @server.tool(name="addSuppliers")
    def add_suppliers_tool(
        idempotency_key: str, name: str, regions: list[str], status: str = "AVAILABLE"
    ) -> dict[str, Any]:
        """Add a supplier at-most-once per idempotency_key (mirrors the executor)."""
        if not idempotency_key:
            raise ValueError("idempotency_key is required (at-most-once premise)")
        try:
            supplier = add_supplier(name, regions, status, idempotency_key)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return _supplier_dict(supplier)

    @server.tool(name="updateProductDescription")
    def update_product_description_tool(
        idempotency_key: str, product_id: int, description: str
    ) -> dict[str, Any]:
        """Update a product's description at-most-once per idempotency_key."""
        product = update_product_description(product_id, description, idempotency_key)
        if product is None:
            raise ValueError(f"Product with id {product_id!r} not found")
        return {"success": True}

    @server.tool(name="updateProductSubstitutes")
    def update_product_substitutes_tool(
        idempotency_key: str, product_id: int, substitute_name: str
    ) -> dict[str, Any]:
        """Point a product at a substitute by name at-most-once per key."""
        try:
            product = update_product_substitutes(product_id, substitute_name, idempotency_key)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if product is None:
            raise ValueError(f"Product with id {product_id!r} not found")
        return {"success": True}

    @server.tool(name="removeProductByName")
    def remove_product_by_name_tool(idempotency_key: str, name: str) -> dict[str, Any]:
        """Remove a product by name at-most-once per idempotency_key."""
        removed = remove_product_by_name(name, idempotency_key)
        if removed is None:
            raise ValueError(f"Product '{name}' not found")
        return _product_dict(removed)

    @server.tool(name="removeProductById")
    def remove_product_by_id_tool(idempotency_key: str, product_id: int) -> dict[str, Any]:
        """Remove a product by id at-most-once per idempotency_key."""
        removed = remove_product_by_id(product_id, idempotency_key)
        if removed is None:
            raise ValueError(f"Product with id {product_id!r} not found")
        return _product_dict(removed)

    @server.tool(name="deleteSupplierByName")
    def delete_supplier_by_name_tool(idempotency_key: str, name: str) -> dict[str, Any]:
        """Delete a supplier by name at-most-once per idempotency_key."""
        removed = delete_supplier_by_name(name, idempotency_key)
        if removed is None:
            raise ValueError(f"Supplier '{name}' not found")
        return {"success": True}

    @server.tool(name="deleteSupplierById")
    def delete_supplier_by_id_tool(idempotency_key: str, supplier_id: int) -> dict[str, Any]:
        """Delete a supplier by id at-most-once per idempotency_key."""
        removed = delete_supplier_by_id(supplier_id, idempotency_key)
        if removed is None:
            raise ValueError(f"Supplier with id {supplier_id!r} not found")
        return {"success": True}

    @server.tool(name="getOrderByOrderId")
    def get_order_by_order_id(order_id: str) -> dict[str, Any]:
        """Return an order by its order_id; raises ValueError when unknown."""
        order = get_by_id(order_id)
        if order is None:
            raise ValueError(f"Order '{order_id}' not found")
        return _order_to_dict(order)

    @server.tool(name="getOrdersBySupplierId")
    def get_orders_by_supplier_id(supplier_id: int) -> dict[str, Any]:
        """Return orders delivered by *supplier_id*, newest first (possibly empty)."""
        return _orders_data(get_orders_by_supplier(supplier_id))

    @server.tool(name="getByProductId")
    def get_by_product_id(product_id: int) -> dict[str, Any]:
        """Return orders for *product_id*, newest first (possibly empty)."""
        return _orders_data(get_orders_by_product(product_id))

    @server.tool(name="getByOrderStatus")
    def get_by_order_status(status: str) -> dict[str, Any]:
        """Return orders in *status*, newest first (possibly empty)."""
        return _orders_data(get_orders_by_status(status))

    @server.tool(name="getByTimeRange")
    def get_by_time_range(start_date: str, end_date: str) -> dict[str, Any]:
        """Return orders created in [start_date, end_date], newest first."""
        return _orders_data(get_orders_by_time_range(start_date, end_date))

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
