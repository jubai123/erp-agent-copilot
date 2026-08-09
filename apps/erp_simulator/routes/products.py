"""Product and inventory endpoints for the ERP Simulator."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from apps.erp_simulator.data.products import PRODUCT_BY_ID, PRODUCT_BY_NAME
from apps.erp_simulator.scenarios import get_scenario

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/{name}")
async def get_product_by_name(name: str) -> dict:
    """Return product info by name."""
    product = PRODUCT_BY_NAME.get(name)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product '{name}' not found")
    stock = 0 if get_scenario() == "stock_insufficient" else product.quantity_in_stock
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        "price": product.price,
        "stock": stock,
        "unit": product.unit,
    }


@router.get("/{product_id}/stock")
async def get_product_stock(product_id: int) -> dict:
    """Return current stock quantity for a product by ID."""
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product with id {product_id} not found")
    stock = 0 if get_scenario() == "stock_insufficient" else product.quantity_in_stock
    return {
        "product_id": product.product_id,
        "name": product.name,
        "stock": stock,
        "unit": product.unit,
    }
