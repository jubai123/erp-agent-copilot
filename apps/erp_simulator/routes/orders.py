"""Order endpoints for the ERP Simulator."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from apps.erp_simulator.data.orders import create_order, get_by_id, get_by_idempotency_key
from apps.erp_simulator.data.products import PRODUCT_BY_ID
from apps.erp_simulator.scenarios import get_scenario


class CreateOrderRequest(BaseModel):
    product_id: int
    quantity: int
    supplier_id: int
    region: str
    idempotency_key: str


router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("")
async def post_order(body: CreateOrderRequest, response: Response) -> dict:
    """Create an order with stock validation and idempotency."""

    if get_scenario() == "timeout":
        raise HTTPException(status_code=504, detail="Order creation timeout")

    existing = get_by_idempotency_key(body.idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _order_to_dict(existing)

    product = PRODUCT_BY_ID.get(body.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product with id {body.product_id} not found")

    effective_stock = 0 if get_scenario() == "stock_insufficient" else product.quantity_in_stock

    if body.quantity > effective_stock:
        detail = (
            f"Insufficient stock: requested {body.quantity} "
            f"but only {effective_stock} available"
        )
        raise HTTPException(status_code=422, detail=detail)

    product.quantity_in_stock -= body.quantity
    order = create_order(
        product_id=product.product_id,
        product_name=product.name,
        quantity=body.quantity,
        supplier_id=body.supplier_id,
        region=body.region,
        unit_price=product.price,
        idempotency_key=body.idempotency_key,
    )
    response.status_code = 201
    return _order_to_dict(order)


@router.get("/{order_id}")
async def get_order(order_id: str) -> dict:
    """Return order details by ID."""
    order = get_by_id(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")
    return _order_to_dict(order)


def _order_to_dict(order) -> dict:
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
