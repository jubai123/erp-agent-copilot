"""Seed supplier data for the ERP Simulator.

In-memory supplier catalog used by the simulator's supplier
endpoints. Each supplier covers specific delivery regions with
associated cost, lead time, and quality metrics.

Runtime mutations (add/delete) live in the fast index (``SUPPLIER_BY_ID`` /
``SUPPLIER_BY_NAME``) and are recorded per idempotency_key so a replay returns
the recorded result without re-applying — the same at-most-once contract as
``orders.create_order``. ``SEED_SUPPLIERS`` stays pristine so catalog-size
invariants (manifest cross-checks) hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Supplier:
    supplier_id: int
    name: str
    regions: list[str] = field(default_factory=list)
    status: str = "AVAILABLE"  # AVAILABLE | UNAVAILABLE
    rating: float = 0.0
    delivery_days: int = 0
    price_per_kg: float = 0.0


SEED_SUPPLIERS: list[Supplier] = [
    Supplier(
        supplier_id=3,
        name="华东物流",
        regions=["上海", "南京"],
        status="AVAILABLE",
        rating=4.8,
        delivery_days=2,
        price_per_kg=1.2,
    ),
    Supplier(
        supplier_id=4,
        name="华北物流",
        regions=["北京", "天津"],
        status="AVAILABLE",
        rating=4.5,
        delivery_days=3,
        price_per_kg=1.0,
    ),
    Supplier(
        supplier_id=5,
        name="华南物流",
        regions=["广州", "深圳"],
        status="UNAVAILABLE",
        rating=4.2,
        delivery_days=1,
        price_per_kg=1.5,
    ),
    Supplier(
        supplier_id=6,
        name="西南物流",
        regions=["成都", "重庆"],
        status="AVAILABLE",
        rating=4.0,
        delivery_days=3,
        price_per_kg=1.3,
    ),
    Supplier(
        supplier_id=7,
        name="西北物流",
        regions=["西安", "兰州"],
        status="AVAILABLE",
        rating=3.8,
        delivery_days=5,
        price_per_kg=0.9,
    ),
]

# Fast lookup index built from the seed list
SUPPLIER_BY_NAME: dict[str, Supplier] = {s.name: s for s in SEED_SUPPLIERS}
SUPPLIER_BY_ID: dict[int, Supplier] = {s.supplier_id: s for s in SEED_SUPPLIERS}

# Write idempotency for catalog mutations, kept separate from the index: a
# replay must return the recorded result without re-applying (add would
# otherwise fabricate a second supplier, delete would re-delete a ghost).
_SUPPLIER_WRITES: dict[str, Supplier] = {}


def add_supplier(
    name: str,
    regions: list[str],
    status: str,
    idempotency_key: str,
) -> Supplier:
    """Add a supplier to the catalog at-most-once per *idempotency_key*.

    A replay (same key) returns the recorded supplier without re-adding. A
    duplicate *name* is a business violation (the name index is a uniqueness
    constraint) and raises ValueError.
    """
    existing = _SUPPLIER_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    if name in SUPPLIER_BY_NAME:
        raise ValueError(f"Supplier '{name}' already exists")
    supplier = Supplier(
        supplier_id=max(SUPPLIER_BY_ID) + 1,
        name=name,
        regions=list(regions or []),
        status=status,
    )
    SUPPLIER_BY_ID[supplier.supplier_id] = supplier
    SUPPLIER_BY_NAME[name] = supplier
    _SUPPLIER_WRITES[idempotency_key] = supplier
    return supplier


def delete_supplier_by_name(name: str, idempotency_key: str) -> Supplier | None:
    """Delete a supplier by name at-most-once per *idempotency_key*.

    Returns None when the supplier does not exist.
    """
    existing = _SUPPLIER_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    supplier = SUPPLIER_BY_NAME.get(name)
    if supplier is None:
        return None
    del SUPPLIER_BY_NAME[name]
    del SUPPLIER_BY_ID[supplier.supplier_id]
    _SUPPLIER_WRITES[idempotency_key] = supplier
    return supplier


def delete_supplier_by_id(supplier_id: int, idempotency_key: str) -> Supplier | None:
    """Delete a supplier by id at-most-once per *idempotency_key*.

    Returns None when the supplier does not exist.
    """
    existing = _SUPPLIER_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    supplier = SUPPLIER_BY_ID.get(supplier_id)
    if supplier is None:
        return None
    del SUPPLIER_BY_ID[supplier_id]
    del SUPPLIER_BY_NAME[supplier.name]
    _SUPPLIER_WRITES[idempotency_key] = supplier
    return supplier
