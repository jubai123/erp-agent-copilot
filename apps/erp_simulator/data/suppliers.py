"""Seed supplier data for the ERP Simulator.

In-memory supplier catalog used by the simulator's supplier
endpoints. Each supplier covers specific delivery regions with
associated cost, lead time, and quality metrics.
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
