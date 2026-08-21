"""Seed product data for the ERP Simulator.

In-memory product catalog used by the simulator's product and
inventory endpoints. Written operations (create/update) modify
this list at runtime for the lifetime of the process.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Product:
    product_id: int
    name: str
    description: str
    price: float
    quantity_in_stock: int
    unit: str
    # The single substitute product (V5 Product.substituteProductId). None
    # means no substitute; the field is populated for the "缺货买什么" flow.
    substitute_product_id: int | None = None


SEED_PRODUCTS: list[Product] = [
    Product(
        product_id=1,
        name="苹果",
        description="新鲜红富士苹果",
        price=10.0,
        quantity_in_stock=100,
        unit="KG",
        substitute_product_id=2,
    ),
    Product(
        product_id=2,
        name="香蕉",
        description="进口香蕉",
        price=8.0,
        quantity_in_stock=50,
        unit="KG",
        substitute_product_id=3,
    ),
    Product(
        product_id=3,
        name="橙子",
        description="赣南脐橙",
        price=12.0,
        quantity_in_stock=80,
        unit="KG",
    ),
    Product(
        product_id=4,
        name="电脑",
        description="办公笔记本电脑",
        price=5000.0,
        quantity_in_stock=15,
        unit="台",
        substitute_product_id=5,
    ),
    Product(
        product_id=5,
        name="键盘",
        description="机械键盘",
        price=200.0,
        quantity_in_stock=30,
        unit="件",
    ),
    Product(
        product_id=6,
        name="鼠标",
        description="无线鼠标",
        price=100.0,
        quantity_in_stock=50,
        unit="件",
    ),
]

# Fast lookup index built from the seed list
PRODUCT_BY_NAME: dict[str, Product] = {p.name: p for p in SEED_PRODUCTS}
PRODUCT_BY_ID: dict[int, Product] = {p.product_id: p for p in SEED_PRODUCTS}


def get_substitutes(product: Product) -> list[Product]:
    """Return the product's substitute (empty when none)."""
    if product.substitute_product_id is None:
        return []
    substitute = PRODUCT_BY_ID.get(product.substitute_product_id)
    return [] if substitute is None else [substitute]


def get_products_by_id_range(start_id: int, end_id: int) -> list[Product]:
    """Return products with product_id in [start_id, end_id], in id order.

    An empty or reversed range yields an empty list (the cloud returns the same
    — a batch query over no ids is an empty result, not an error).
    """
    if end_id < start_id:
        return []
    return [p for p in SEED_PRODUCTS if start_id <= p.product_id <= end_id]
