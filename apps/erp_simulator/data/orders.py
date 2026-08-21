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
# Write idempotency for status mutations, kept separate from the create store:
# an update/cancel mutates an existing order (no new record to key off), so a
# replay must return the recorded result without re-applying.
_order_writes: dict[str, Order] = {}

# Legal transitions mirror rules.yaml order-state-machine: one step forward in
# CREATED → CONFIRMED → SHIPPED → DELIVERED, or into CANCELLED from any
# non-terminal state. Terminal states and rollback/skip are rejected.
_ORDER_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"CONFIRMED", "CANCELLED"}),
    "CONFIRMED": frozenset({"SHIPPED", "CANCELLED"}),
    "SHIPPED": frozenset({"DELIVERED", "CANCELLED"}),
    "DELIVERED": frozenset(),
    "CANCELLED": frozenset(),
}


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


def can_transition(current: str, target: str) -> bool:
    """Whether rules.yaml order-state-machine allows *current* -> *target*."""
    return target in _ORDER_STATE_TRANSITIONS.get(current, frozenset())


def update_order_status(order_id: str, new_status: str, idempotency_key: str) -> Order | None:
    """Apply a status change at-most-once per *idempotency_key*.

    A replay (same key) returns the recorded order without re-applying — the
    same at-most-once contract as ``create_order``. Returns None when the order
    does not exist; raises ValueError for an illegal transition (terminal state,
    rollback to CREATED, or a skip-level jump).
    """
    existing = _order_writes.get(idempotency_key)
    if existing is not None:
        return existing
    order = _orders_by_id.get(order_id)
    if order is None:
        return None
    if not can_transition(order.status, new_status):
        raise ValueError(
            f"Order '{order_id}' cannot transition from {order.status} to {new_status}"
        )
    order.status = new_status
    _order_writes[idempotency_key] = order
    return order


def cancel_order(order_id: str, idempotency_key: str) -> Order | None:
    """Cancel an order at-most-once per *idempotency_key*.

    Mirrors ``update_order_status``; only CREATED/CONFIRMED/SHIPPED orders may
    reach CANCELLED (rules.yaml order-cancel-constraint). Returns None when the
    order does not exist; raises ValueError for a terminal order.
    """
    existing = _order_writes.get(idempotency_key)
    if existing is not None:
        return existing
    order = _orders_by_id.get(order_id)
    if order is None:
        return None
    if not can_transition(order.status, "CANCELLED"):
        raise ValueError(f"Order '{order_id}' in status {order.status} cannot be cancelled")
    order.status = "CANCELLED"
    _order_writes[idempotency_key] = order
    return order


def get_orders_by_supplier(supplier_id: int) -> list[Order]:
    """Return orders delivered by *supplier_id*, newest first."""
    orders = [o for o in _orders_by_id.values() if o.supplier_id == supplier_id]
    return _by_recency(orders)


def get_orders_by_product(product_id: int) -> list[Order]:
    """Return orders for *product_id*, newest first."""
    orders = [o for o in _orders_by_id.values() if o.product_id == product_id]
    return _by_recency(orders)


def get_orders_by_status(status: str) -> list[Order]:
    """Return orders in *status*, newest first."""
    orders = [o for o in _orders_by_id.values() if o.status == status]
    return _by_recency(orders)


def get_orders_by_time_range(start_date: str, end_date: str) -> list[Order]:
    """Return orders created in [start_date, end_date] (inclusive), newest first.

    Both bounds are ISO-8601 strings; the date-only form ("2023-01-01") is
    interpreted as midnight UTC. Order statuses are compared against the V6
    enum (CREATED/CONFIRMED/SHIPPED/DELIVERED/CANCELLED).
    """
    start = _parse_iso(start_date)
    end = _parse_iso(end_date)
    if start is None or end is None:
        return []
    orders = [o for o in _orders_by_id.values() if _created_between(o, start, end)]
    return _by_recency(orders)


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; tolerate a missing offset (assume UTC)."""
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _created_between(order: Order, start: datetime, end: datetime) -> bool:
    created = _parse_iso(order.created_at)
    return created is not None and start <= created <= end


def _by_recency(orders: list[Order]) -> list[Order]:
    """Sort orders newest-first by created_at, then by order_id for determinism."""
    return sorted(orders, key=lambda o: (o.created_at, o.order_id), reverse=True)
