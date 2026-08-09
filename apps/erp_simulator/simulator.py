"""ERP Simulator — FastAPI entry point.

Provides a standalone FastAPI application that simulates an ERP
backend with deterministic product, inventory, supplier, and order
endpoints. Written to avoid dependency on live external ERP systems.
"""

from __future__ import annotations

from fastapi import FastAPI

from apps.erp_simulator.routes.orders import router as orders_router
from apps.erp_simulator.routes.products import router as products_router
from apps.erp_simulator.routes.suppliers import router as suppliers_router
from apps.erp_simulator.scenarios import router as scenarios_router


def create_simulator_app() -> FastAPI:
    """Create a FastAPI application for the ERP Simulator."""
    app = FastAPI(title="ERP Agent Copilot - ERP Simulator", version="0.1.0")

    app.include_router(products_router)
    app.include_router(suppliers_router)
    app.include_router(orders_router)
    app.include_router(scenarios_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "erp-simulator"}

    return app
