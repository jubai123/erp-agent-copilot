"""In-memory order store for the ERP Simulator.

Orders are indexed by both order_id and idempotency_key for
fast lookup during creation and retrieval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Order:
    order_id: str
    product_id: int
    product_name: str
    quantity: int
    supplier_id: int
    region: str
    amount: float
    status: str
    idempotency_key: str
    created_at: str


# In-memory stores — shared across requests within the process lifetime.
_orders_by_id: dict[str, Order] = {}
_orders_by_idempotency: dict[str, Order] = {}


def create_order(
    product_id: int,
    product_name: str,
    quantity: int,
    supplier_id: int,
    region: str,
    unit_price: float,
    idempotency_key: str,
) -> Order:
    """Create and store a new order. Returns the Order instance."""
    order_id = uuid.uuid4().hex[:12]
    created_at = datetime.now(UTC).isoformat()
    order = Order(
        order_id=order_id,
        product_id=product_id,
        product_name=product_name,
        quantity=quantity,
        supplier_id=supplier_id,
        region=region,
        amount=round(unit_price * quantity, 2),
        status="CREATED",
        idempotency_key=idempotency_key,
        created_at=created_at,
    )
    _orders_by_id[order_id] = order
    _orders_by_idempotency[idempotency_key] = order
    return order


def get_by_idempotency_key(key: str) -> Order | None:
    return _orders_by_idempotency.get(key)


def get_by_id(order_id: str) -> Order | None:
    return _orders_by_id.get(order_id)
