"""Seed product data for the ERP Simulator.

In-memory product catalog used by the simulator's product and
inventory endpoints. Written operations (create/update) modify
this list at runtime for the lifetime of the process.

Runtime mutations (add/update/remove) live in the fast index
(``PRODUCT_BY_ID`` / ``PRODUCT_BY_NAME``) and are recorded per
idempotency_key so a replay returns the recorded result without re-applying —
the same at-most-once contract as ``orders.create_order``. ``SEED_PRODUCTS``
stays pristine so catalog-size invariants (manifest cross-checks) hold.
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

# Write idempotency for catalog mutations, kept separate from the index: a
# replay must return the recorded result without re-applying (add would
# otherwise fabricate a second product, remove would re-delete a ghost).
_PRODUCT_WRITES: dict[str, Product] = {}


def get_substitutes(product: Product) -> list[Product]:
    """Return the product's substitute (empty when none)."""
    if product.substitute_product_id is None:
        return []
    substitute = PRODUCT_BY_ID.get(product.substitute_product_id)
    return [] if substitute is None else [substitute]


def get_products_by_id_range(start_id: int, end_id: int) -> list[Product]:
    """Return products with product_id in [start_id, end_id], in id order.

    Sourced from the live index (not the seed list) so a runtime-added product
    is visible to a batch query. An empty or reversed range yields an empty
    list (the cloud returns the same — a batch query over no ids is an empty
    result, not an error).
    """
    if end_id < start_id:
        return []
    return sorted(
        (p for p in PRODUCT_BY_ID.values() if start_id <= p.product_id <= end_id),
        key=lambda p: p.product_id,
    )


def add_product(
    name: str,
    description: str,
    price: float,
    quantity_in_stock: int,
    idempotency_key: str,
) -> Product:
    """Add a product to the catalog at-most-once per *idempotency_key*.

    A replay (same key) returns the recorded product without re-adding. A
    duplicate *name* is a business violation (the name index is a uniqueness
    constraint) and raises ValueError.
    """
    existing = _PRODUCT_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    if name in PRODUCT_BY_NAME:
        raise ValueError(f"Product '{name}' already exists")
    product = Product(
        product_id=max(PRODUCT_BY_ID) + 1,
        name=name,
        description=description,
        price=price,
        quantity_in_stock=quantity_in_stock,
        unit="件",
        substitute_product_id=None,
    )
    PRODUCT_BY_ID[product.product_id] = product
    PRODUCT_BY_NAME[name] = product
    _PRODUCT_WRITES[idempotency_key] = product
    return product


def update_product_description(
    product_id: int, description: str, idempotency_key: str
) -> Product | None:
    """Update a product's description at-most-once per *idempotency_key*.

    Returns None when the product does not exist.
    """
    existing = _PRODUCT_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        return None
    product.description = description
    _PRODUCT_WRITES[idempotency_key] = product
    return product


def update_product_substitutes(
    product_id: int, substitute_name: str, idempotency_key: str
) -> Product | None:
    """Point a product at a substitute (by name) at-most-once per idempotency_key.

    Returns None when the target product does not exist; raises ValueError when
    the substitute product is unknown (the V5 contract names the substitute, so
    an unresolvable name is a business violation, not a silent no-op).
    """
    existing = _PRODUCT_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        return None
    substitute = PRODUCT_BY_NAME.get(substitute_name)
    if substitute is None:
        raise ValueError(f"Substitute product '{substitute_name}' not found")
    product.substitute_product_id = substitute.product_id
    _PRODUCT_WRITES[idempotency_key] = product
    return product


def remove_product_by_name(name: str, idempotency_key: str) -> Product | None:
    """Remove a product by name at-most-once per *idempotency_key*.

    Returns None when the product does not exist.
    """
    existing = _PRODUCT_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    product = PRODUCT_BY_NAME.get(name)
    if product is None:
        return None
    del PRODUCT_BY_NAME[name]
    del PRODUCT_BY_ID[product.product_id]
    _PRODUCT_WRITES[idempotency_key] = product
    return product


def remove_product_by_id(product_id: int, idempotency_key: str) -> Product | None:
    """Remove a product by id at-most-once per *idempotency_key*.

    Returns None when the product does not exist.
    """
    existing = _PRODUCT_WRITES.get(idempotency_key)
    if existing is not None:
        return existing
    product = PRODUCT_BY_ID.get(product_id)
    if product is None:
        return None
    del PRODUCT_BY_ID[product_id]
    del PRODUCT_BY_NAME[product.name]
    _PRODUCT_WRITES[idempotency_key] = product
    return product
