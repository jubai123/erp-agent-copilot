"""Supplier endpoints for the ERP Simulator."""

from __future__ import annotations

from fastapi import APIRouter, Query

from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS, Supplier
from apps.erp_simulator.scenarios import get_scenario

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


@router.get("")
async def get_suppliers_by_region(
    region: str = Query(..., description="Delivery region to search"),
    status: str | None = Query(None, description="Optional status filter"),
) -> list[dict]:
    """Return suppliers serving the given region, optionally filtered by status."""
    scenario = get_scenario()
    results: list[Supplier] = []
    for supplier in SEED_SUPPLIERS:
        if region not in supplier.regions:
            continue
        effective_status = "UNAVAILABLE" if scenario == "supplier_unavailable" else supplier.status
        if status is not None and effective_status != status:
            continue
        results.append(supplier)

    return [
        {
            "supplier_id": s.supplier_id,
            "name": s.name,
            "regions": s.regions,
            "status": "UNAVAILABLE" if scenario == "supplier_unavailable" else s.status,
            "rating": s.rating,
            "delivery_days": s.delivery_days,
            "price_per_kg": s.price_per_kg,
        }
        for s in results
    ]
