"""Cloud-ERP MCP server — real ERP tools over the MCP protocol.

Retires the "MCP 只接模拟数据" gap: this server exposes the same tool contract
as the demo server (docs/05 §5), but every tool is backed by
:func:`apps.worker.executor.build_erp_http_executor`, which calls the real cloud
ERP over HTTP (V5 convention, X-API-Key header) and normalizes the camelCase
responses back to the snake_case shapes the agent validates against. No ERP
logic is duplicated here — this module is a thin adapter that maps the
executor's ``(tool_name, arguments) -> ToolResult`` contract onto MCP tools.

A business failure (unknown product, id:-1 order rejection, HTTP 5xx) raises
ValueError, which the SDK surfaces as a CallToolResult(is_error=True) — the MCP
analogue of ToolResult.failure. Note: MCP has no retry signal, so the
executor's is_retryable flag is not carried across the boundary; a client sees
a permanent-looking is_error (the error_code is embedded in the message).

createOrder is only registered when ``create_order_enabled`` and the catalog
maintenance/delete tools (addProduct, addSuppliers, updateProduct*,
removeProduct*, deleteSupplier*) only when ``maintenance_enabled`` — MCP has no
"disabled tool" state, so gating means not advertising the write path at all
(omit, not reject). Default off: reads are safe; writes need an explicit
operator decision because a real cloud order cannot be deleted.

Run:  python -m apps.mcp_gateway.erp_mcp_server
Then connect a client to:  http://127.0.0.1:8766/mcp
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from apps.worker.executor import build_erp_http_executor
from erp_copilot.infrastructure.config import Settings
from erp_copilot.tools.contract import get_contract
from erp_copilot.tools.tool_result import ToolResult

HOST = "127.0.0.1"
PORT = 8766
MCP_PATH = "/mcp"

_Executor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


def _result_to_mcp(result: ToolResult) -> dict[str, Any]:
    """Map a ToolResult onto an MCP tool response.

    SUCCEEDED -> the data dict (empty dict when data is None); FAILED -> raise
    ValueError so the SDK returns is_error=True with the error_code visible.
    """
    if result.status == "FAILED" and result.error is not None:
        raise ValueError(f"{result.error.error_code}: {result.error.error_message}")
    return result.data or {}


def _tool_description(name: str) -> str:
    """Return the contract's selection prose for *name* (the MCP description).

    The MCP surface is a consumer of the tool-contract authority: what a client
    sees as a tool's description is the contract's prose (the seed, or an
    operator's DB override), never a hand-written docstring. A tool with no
    contract degrades to its name so it still lists.
    """
    contract = get_contract(name)
    return contract.description if contract is not None else name


def build_erp_mcp_server(
    executor: _Executor,
    *,
    create_order_enabled: bool = False,
    maintenance_enabled: bool = False,
) -> MCPServer:
    """Build an MCPServer exposing the cloud ERP tools over MCP.

    *executor* is the ``(tool_name, arguments) -> ToolResult`` seam — production
    passes ``build_erp_http_executor(...)``; tests inject a stub or let respx
    mock the cloud. ``create_order_enabled`` gates the order write tools and
    ``maintenance_enabled`` gates the catalog maintenance/delete tools (both
    omit when off — MCP has no disabled-tool state).
    """
    server = MCPServer(
        name="erp-cloud",
        version="0.1.0",
        instructions=(
            "Real cloud ERP tools over MCP (V5 HTTP convention). Reads hit the "
            "live ERP; writes are only advertised when explicitly enabled."
        ),
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "erp-cloud-mcp"})

    @server.tool(name="getProductByName", description=_tool_description("getProductByName"))
    async def get_product_by_name(name: str) -> dict[str, Any]:
        """Return a product's catalog fields by name (real ERP)."""
        return _result_to_mcp(await executor("getProductByName", {"name": name}))

    @server.tool(name="getProductById", description=_tool_description("getProductById"))
    async def get_product_by_id(product_id: int) -> dict[str, Any]:
        """Return a product's catalog fields by id (real ERP)."""
        return _result_to_mcp(await executor("getProductById", {"product_id": product_id}))

    @server.tool(
        name="getProductSubstitutes", description=_tool_description("getProductSubstitutes")
    )
    async def get_product_substitutes(product_id: int) -> dict[str, Any]:
        """Return a product's substitutes by id (real ERP), possibly empty."""
        return _result_to_mcp(await executor("getProductSubstitutes", {"product_id": product_id}))

    @server.tool(
        name="getProductSubstitutesByName",
        description=_tool_description("getProductSubstitutesByName"),
    )
    async def get_product_substitutes_by_name(name: str) -> dict[str, Any]:
        """Return a product's substitutes by name (real ERP), possibly empty."""
        return _result_to_mcp(await executor("getProductSubstitutesByName", {"name": name}))

    @server.tool(
        name="getBatchProductByProductIds",
        description=_tool_description("getBatchProductByProductIds"),
    )
    async def get_batch_product_by_product_ids(start_id: int, end_id: int) -> dict[str, Any]:
        """Return products whose id falls in [start_id, end_id] (real ERP)."""
        return _result_to_mcp(
            await executor(
                "getBatchProductByProductIds",
                {"start_id": start_id, "end_id": end_id},
            )
        )

    @server.tool(name="getSupplierByStatus", description=_tool_description("getSupplierByStatus"))
    async def get_supplier_by_status(status: str = "AVAILABLE") -> dict[str, Any]:
        """Return suppliers with *status* (default AVAILABLE), first as supplier_id."""
        return _result_to_mcp(await executor("getSupplierByStatus", {"status": status}))

    @server.tool(
        name="querySuppliersByDeliveryRegion",
        description=_tool_description("querySuppliersByDeliveryRegion"),
    )
    async def query_suppliers_by_delivery_region(region: str) -> dict[str, Any]:
        """Return suppliers covering *region*, with the first as ``supplier_id``."""
        return _result_to_mcp(await executor("querySuppliersByDeliveryRegion", {"region": region}))

    @server.tool(name="getSupplierByName", description=_tool_description("getSupplierByName"))
    async def get_supplier_by_name(name: str) -> dict[str, Any]:
        """Return a single supplier's fields by name (real ERP)."""
        return _result_to_mcp(await executor("getSupplierByName", {"name": name}))

    @server.tool(name="getSupplierById", description=_tool_description("getSupplierById"))
    async def get_supplier_by_id(supplier_id: int) -> dict[str, Any]:
        """Return a single supplier's fields by id (real ERP)."""
        return _result_to_mcp(await executor("getSupplierById", {"supplier_id": supplier_id}))

    if create_order_enabled:

        @server.tool(name="createOrder", description=_tool_description("createOrder"))
        async def create_order_tool(
            idempotency_key: str,
            product_id: int,
            quantity: int,
            supplier_id: int,
            region: str,
        ) -> dict[str, Any]:
            """Create an order on the real ERP.

            The cloud createOrder API has no idempotency-key field, so the key
            is accepted for contract parity but dropped by the executor;
            at-most-once must be provided by the caller (e.g. the worker's
            IdempotencyStore).
            """
            return _result_to_mcp(
                await executor(
                    "createOrder",
                    {
                        "idempotency_key": idempotency_key,
                        "product_id": product_id,
                        "quantity": quantity,
                        "supplier_id": supplier_id,
                        "region": region,
                    },
                )
            )

        @server.tool(name="updateOrderStatus", description=_tool_description("updateOrderStatus"))
        async def update_order_status_tool(
            order_id: str, status: str, idempotency_key: str
        ) -> dict[str, Any]:
            """Advance an order's status on the real ERP (gated write).

            The cloud API has no idempotency-key field, so the key is accepted
            for contract parity but dropped by the executor; at-most-once must be
            provided by the caller. Illegal transitions (terminal order,
            rollback, skip-level) surface as an MCP error result.
            """
            return _result_to_mcp(
                await executor(
                    "updateOrderStatus",
                    {
                        "order_id": order_id,
                        "status": status,
                        "idempotency_key": idempotency_key,
                    },
                )
            )

        @server.tool(name="cancelOrder", description=_tool_description("cancelOrder"))
        async def cancel_order_tool(order_id: str, idempotency_key: str) -> dict[str, Any]:
            """Cancel an order on the real ERP (gated write).

            Only CREATED/CONFIRMED/SHIPPED orders may be cancelled; a terminal
            order surfaces as an MCP error result. At-most-once must be provided
            by the caller (no cloud idempotency field).
            """
            return _result_to_mcp(
                await executor(
                    "cancelOrder",
                    {"order_id": order_id, "idempotency_key": idempotency_key},
                )
            )

    if maintenance_enabled:

        @server.tool(name="addProduct", description=_tool_description("addProduct"))
        async def add_product_tool(
            idempotency_key: str,
            name: str,
            price: float,
            quantity_in_stock: int,
            description: str = "",
        ) -> dict[str, Any]:
            """Add a product to the catalog (gated write, DANGEROUS delete)."""
            return _result_to_mcp(
                await executor(
                    "addProduct",
                    {
                        "idempotency_key": idempotency_key,
                        "name": name,
                        "price": price,
                        "quantity_in_stock": quantity_in_stock,
                        "description": description,
                    },
                )
            )

        @server.tool(name="addSuppliers", description=_tool_description("addSuppliers"))
        async def add_suppliers_tool(
            idempotency_key: str,
            name: str,
            regions: list[str],
            status: str = "AVAILABLE",
        ) -> dict[str, Any]:
            """Add a supplier to the catalog (gated write)."""
            return _result_to_mcp(
                await executor(
                    "addSuppliers",
                    {
                        "idempotency_key": idempotency_key,
                        "name": name,
                        "regions": regions,
                        "status": status,
                    },
                )
            )

        @server.tool(
            name="updateProductDescription",
            description=_tool_description("updateProductDescription"),
        )
        async def update_product_description_tool(
            idempotency_key: str, product_id: int, description: str
        ) -> dict[str, Any]:
            """Update a product's description (gated write)."""
            return _result_to_mcp(
                await executor(
                    "updateProductDescription",
                    {
                        "idempotency_key": idempotency_key,
                        "product_id": product_id,
                        "description": description,
                    },
                )
            )

        @server.tool(
            name="updateProductSubstitutes",
            description=_tool_description("updateProductSubstitutes"),
        )
        async def update_product_substitutes_tool(
            idempotency_key: str, product_id: int, substitute_name: str
        ) -> dict[str, Any]:
            """Point a product at a substitute by name (gated write)."""
            return _result_to_mcp(
                await executor(
                    "updateProductSubstitutes",
                    {
                        "idempotency_key": idempotency_key,
                        "product_id": product_id,
                        "substitute_name": substitute_name,
                    },
                )
            )

        @server.tool(
            name="removeProductByName", description=_tool_description("removeProductByName")
        )
        async def remove_product_by_name_tool(idempotency_key: str, name: str) -> dict[str, Any]:
            """Remove a product by name (gated DANGEROUS delete)."""
            return _result_to_mcp(
                await executor(
                    "removeProductByName",
                    {"idempotency_key": idempotency_key, "name": name},
                )
            )

        @server.tool(name="removeProductById", description=_tool_description("removeProductById"))
        async def remove_product_by_id_tool(
            idempotency_key: str, product_id: int
        ) -> dict[str, Any]:
            """Remove a product by id (gated DANGEROUS delete)."""
            return _result_to_mcp(
                await executor(
                    "removeProductById",
                    {"idempotency_key": idempotency_key, "product_id": product_id},
                )
            )

        @server.tool(
            name="deleteSupplierByName", description=_tool_description("deleteSupplierByName")
        )
        async def delete_supplier_by_name_tool(idempotency_key: str, name: str) -> dict[str, Any]:
            """Delete a supplier by name (gated DANGEROUS delete)."""
            return _result_to_mcp(
                await executor(
                    "deleteSupplierByName",
                    {"idempotency_key": idempotency_key, "name": name},
                )
            )

        @server.tool(name="deleteSupplierById", description=_tool_description("deleteSupplierById"))
        async def delete_supplier_by_id_tool(
            idempotency_key: str, supplier_id: int
        ) -> dict[str, Any]:
            """Delete a supplier by id (gated DANGEROUS delete)."""
            return _result_to_mcp(
                await executor(
                    "deleteSupplierById",
                    {"idempotency_key": idempotency_key, "supplier_id": supplier_id},
                )
            )

    @server.tool(name="getOrderByOrderId", description=_tool_description("getOrderByOrderId"))
    async def get_order_by_order_id(order_id: str) -> dict[str, Any]:
        """Return an order by its order_id (real ERP)."""
        return _result_to_mcp(await executor("getOrderByOrderId", {"order_id": order_id}))

    @server.tool(
        name="getOrdersBySupplierId", description=_tool_description("getOrdersBySupplierId")
    )
    async def get_orders_by_supplier_id(supplier_id: int) -> dict[str, Any]:
        """Return orders delivered by *supplier_id*, newest first (possibly empty)."""
        return _result_to_mcp(await executor("getOrdersBySupplierId", {"supplier_id": supplier_id}))

    @server.tool(name="getByProductId", description=_tool_description("getByProductId"))
    async def get_by_product_id(product_id: int) -> dict[str, Any]:
        """Return orders for *product_id*, newest first (possibly empty)."""
        return _result_to_mcp(await executor("getByProductId", {"product_id": product_id}))

    @server.tool(name="getByOrderStatus", description=_tool_description("getByOrderStatus"))
    async def get_by_order_status(status: str) -> dict[str, Any]:
        """Return orders in *status*, newest first (possibly empty)."""
        return _result_to_mcp(await executor("getByOrderStatus", {"status": status}))

    @server.tool(name="getByTimeRange", description=_tool_description("getByTimeRange"))
    async def get_by_time_range(start_date: str, end_date: str) -> dict[str, Any]:
        """Return orders created in [start_date, end_date], newest first."""
        return _result_to_mcp(
            await executor("getByTimeRange", {"start_date": start_date, "end_date": end_date})
        )

    return server


def build_erp_mcp_app(settings: Settings) -> Starlette:
    """Return the ASGI app serving the cloud-ERP MCP server over Streamable HTTP.

    Reads the cloud ERP credentials from Settings: ``erp_api_base_url`` /
    ``erp_api_key`` (X-API-Key header) and the ``erp_mcp_create_order_enabled`` /
    ``erp_mcp_maintenance_enabled`` write gates. An empty base URL yields a
    server whose tools fail — the operator must configure the cloud endpoint.
    """
    executor = build_erp_http_executor(
        settings.erp_api_base_url,
        settings.erp_api_key.get_secret_value(),
    )
    return build_erp_mcp_server(
        executor,
        create_order_enabled=settings.erp_mcp_create_order_enabled,
        maintenance_enabled=settings.erp_mcp_maintenance_enabled,
    ).streamable_http_app(streamable_http_path=MCP_PATH, host=HOST)


if __name__ == "__main__":
    import uvicorn

    from erp_copilot.infrastructure.database import get_session, init_db
    from erp_copilot.tools.contract import ensure_contracts_loaded

    settings = Settings()  # type: ignore[call-arg]
    init_db(settings)
    ensure_contracts_loaded(get_session())
    uvicorn.run(build_erp_mcp_app(settings), host=HOST, port=PORT)  # type: ignore[call-arg]
