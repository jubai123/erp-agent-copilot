"""Unit tests for the product catalog mutation data layer.

apps/erp_simulator/data/products add/update/remove functions mirror the
order-write at-most-once contract: each write is recorded per idempotency_key
and a replay returns the recorded product without re-applying. Illegal writes
(duplicate name, unknown substitute, unknown product) are honest failures, not
silent partial writes. Runtime-mutated products live in the fast index
(PRODUCT_BY_ID / PRODUCT_BY_NAME), so batch reads see them; the seed list stays
pristine so catalog-size invariants keep holding.
"""

from __future__ import annotations

import uuid

import pytest

from apps.erp_simulator.data.products import (
    PRODUCT_BY_ID,
    PRODUCT_BY_NAME,
    add_product,
    get_products_by_id_range,
    remove_product_by_id,
    remove_product_by_name,
    update_product_description,
    update_product_substitutes,
)


def _new_product_name() -> str:
    return f"测试商品-{uuid.uuid4().hex[:8]}"


def _create_product() -> tuple[str, int]:
    name = _new_product_name()
    product = add_product(
        name=name,
        description="测试描述",
        price=9.9,
        quantity_in_stock=5,
        idempotency_key=f"add-{name}",
    )
    return name, product.product_id


def _new_key() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


class TestAddProduct:
    def test_add_creates_indexed_product(self) -> None:
        name = _new_product_name()

        product = add_product(name, "测试", 9.9, 5, f"add-{name}")

        assert PRODUCT_BY_ID[product.product_id] is product
        assert PRODUCT_BY_NAME[name] is product
        assert product.quantity_in_stock == 5
        assert product.substitute_product_id is None

    def test_replay_returns_recorded_without_reapplying(self) -> None:
        name = _new_product_name()
        key = _new_key()
        first = add_product(name, "描述A", 9.9, 5, key)

        replayed = add_product("其他名称", "描述B", 1.0, 1, key)

        assert replayed is first
        assert replayed.name == name
        assert PRODUCT_BY_NAME.get("其他名称") is None  # not re-applied

    def test_duplicate_name_is_rejected(self) -> None:
        name = _new_product_name()
        add_product(name, "一", 1.0, 1, f"dup-1-{name}")

        with pytest.raises(ValueError, match="already exists"):
            add_product(name, "二", 2.0, 2, f"dup-2-{name}")

    def test_added_product_visible_in_batch_range(self) -> None:
        name = _new_product_name()
        product = add_product(name, "测试", 9.9, 5, f"add-batch-{name}")

        ids = {
            p.product_id for p in get_products_by_id_range(product.product_id, product.product_id)
        }

        assert ids == {product.product_id}


class TestUpdateProductDescription:
    def test_updates_description(self) -> None:
        name, pid = _create_product()

        updated = update_product_description(pid, "新描述", _new_key())

        assert updated is not None
        assert updated.product_id == pid
        assert updated.description == "新描述"

    def test_replay_returns_recorded_result(self) -> None:
        name, pid = _create_product()
        key = _new_key()
        first = update_product_description(pid, "A", key)

        replayed = update_product_description(pid, "B", key)

        assert replayed is first
        assert replayed.description == "A"  # recorded result, not re-applied

    def test_unknown_product_returns_none(self) -> None:
        assert update_product_description(999999, "x", _new_key()) is None


class TestUpdateProductSubstitutes:
    def test_sets_substitute_by_name(self) -> None:
        name, pid = _create_product()

        updated = update_product_substitutes(pid, "香蕉", _new_key())

        assert updated is not None
        assert updated.substitute_product_id == 2

    def test_unknown_substitute_raises(self) -> None:
        name, pid = _create_product()

        with pytest.raises(ValueError, match="not found"):
            update_product_substitutes(pid, "不存在的替代品", _new_key())

    def test_unknown_product_returns_none(self) -> None:
        assert update_product_substitutes(999999, "香蕉", _new_key()) is None


class TestRemoveProduct:
    def test_remove_by_name_clears_indexes(self) -> None:
        name, pid = _create_product()

        removed = remove_product_by_name(name, f"rmn-{name}")

        assert removed is not None
        assert removed.product_id == pid
        assert PRODUCT_BY_NAME.get(name) is None
        assert PRODUCT_BY_ID.get(pid) is None

    def test_remove_by_id_clears_indexes(self) -> None:
        name, pid = _create_product()

        removed = remove_product_by_id(pid, f"rmi-{pid}")

        assert removed is not None
        assert removed.product_id == pid
        assert PRODUCT_BY_ID.get(pid) is None
        assert PRODUCT_BY_NAME.get(name) is None

    def test_second_remove_returns_none(self) -> None:
        name, pid = _create_product()
        remove_product_by_name(name, f"rm1-{name}")

        assert remove_product_by_name(name, f"rm2-{name}") is None

    def test_replay_returns_recorded_removed_product(self) -> None:
        name, pid = _create_product()
        key = _new_key()
        first = remove_product_by_name(name, key)

        replayed = remove_product_by_name("其他名称", key)

        assert replayed is first
        assert replayed.product_id == pid

    def test_remove_unknown_by_name_returns_none(self) -> None:
        assert remove_product_by_name("不存在的商品", _new_key()) is None

    def test_remove_unknown_by_id_returns_none(self) -> None:
        assert remove_product_by_id(999999, _new_key()) is None
