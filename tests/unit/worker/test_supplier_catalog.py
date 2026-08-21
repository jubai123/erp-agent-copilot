"""Unit tests for the supplier catalog mutation data layer.

apps/erp_simulator/data/suppliers add/delete functions mirror the order-write
at-most-once contract: each write is recorded per idempotency_key and a replay
returns the recorded supplier without re-applying. Duplicate names are rejected
and unknown suppliers delete to None (honest not-found).
"""

from __future__ import annotations

import uuid

import pytest

from apps.erp_simulator.data.suppliers import (
    SUPPLIER_BY_ID,
    SUPPLIER_BY_NAME,
    add_supplier,
    delete_supplier_by_id,
    delete_supplier_by_name,
)


def _new_supplier_name() -> str:
    return f"测试物流-{uuid.uuid4().hex[:8]}"


def _create_supplier() -> tuple[str, int]:
    name = _new_supplier_name()
    supplier = add_supplier(
        name=name,
        regions=["上海"],
        status="AVAILABLE",
        idempotency_key=f"add-{name}",
    )
    return name, supplier.supplier_id


def _new_key() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


class TestAddSupplier:
    def test_add_creates_indexed_supplier(self) -> None:
        name = _new_supplier_name()

        supplier = add_supplier(name, ["上海"], "AVAILABLE", f"add-{name}")

        assert SUPPLIER_BY_NAME[name] is supplier
        assert SUPPLIER_BY_ID[supplier.supplier_id] is supplier
        assert supplier.regions == ["上海"]
        assert supplier.status == "AVAILABLE"

    def test_replay_returns_recorded_without_reapplying(self) -> None:
        name = _new_supplier_name()
        key = _new_key()
        first = add_supplier(name, ["上海"], "AVAILABLE", key)

        replayed = add_supplier("其他名称", ["北京"], "UNAVAILABLE", key)

        assert replayed is first
        assert replayed.name == name
        assert SUPPLIER_BY_NAME.get("其他名称") is None  # not re-applied

    def test_duplicate_name_is_rejected(self) -> None:
        name = _new_supplier_name()
        add_supplier(name, ["上海"], "AVAILABLE", f"dup-1-{name}")

        with pytest.raises(ValueError, match="already exists"):
            add_supplier(name, ["北京"], "AVAILABLE", f"dup-2-{name}")


class TestDeleteSupplier:
    def test_delete_by_name_clears_indexes(self) -> None:
        name, sid = _create_supplier()

        removed = delete_supplier_by_name(name, f"dsn-{name}")

        assert removed is not None
        assert removed.supplier_id == sid
        assert SUPPLIER_BY_NAME.get(name) is None
        assert SUPPLIER_BY_ID.get(sid) is None

    def test_delete_by_id_clears_indexes(self) -> None:
        name, sid = _create_supplier()

        removed = delete_supplier_by_id(sid, f"dsi-{sid}")

        assert removed is not None
        assert removed.supplier_id == sid
        assert SUPPLIER_BY_ID.get(sid) is None
        assert SUPPLIER_BY_NAME.get(name) is None

    def test_second_delete_returns_none(self) -> None:
        name, sid = _create_supplier()
        delete_supplier_by_name(name, f"d1-{name}")

        assert delete_supplier_by_name(name, f"d2-{name}") is None

    def test_replay_returns_recorded_result(self) -> None:
        name, sid = _create_supplier()
        key = _new_key()
        first = delete_supplier_by_name(name, key)

        replayed = delete_supplier_by_name("其他名称", key)

        assert replayed is first
        assert replayed.supplier_id == sid

    def test_delete_unknown_returns_none(self) -> None:
        assert delete_supplier_by_name("不存在的供应商", _new_key()) is None
        assert delete_supplier_by_id(999999, _new_key()) is None
