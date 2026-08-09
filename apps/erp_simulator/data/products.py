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


SEED_PRODUCTS: list[Product] = [
    Product(
        product_id=1,
        name="苹果",
        description="新鲜红富士苹果",
        price=10.0,
        quantity_in_stock=100,
        unit="KG",
    ),
    Product(
        product_id=2,
        name="香蕉",
        description="进口香蕉",
        price=8.0,
        quantity_in_stock=50,
        unit="KG",
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
