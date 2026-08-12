"""Unit tests for agent/recovery_decision.py — deterministic recovery action.

``decide_recovery_action`` maps a natural-language query to one of four recovery
actions (ask_missing / confirm_conflict / retry / reject) using the same
deterministic, offline rules the agent runtime will reuse (docs/06 §7). The
datasets/recovery_25.json labels are the ground-truth contract: every query must
trigger its labeled action.

Local domain catalogs mirror the ERP Simulator seed data
(apps/erp_simulator/data) exactly as classify_intent mirrors manifest.yaml; the
mirror tests guard against drift. No LLM, no DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.erp_simulator.data.products import PRODUCT_BY_NAME
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS
from erp_copilot.agent.recovery_decision import (
    _PRODUCT_STOCK,
    _REGION_SUPPLIER,
    RecoveryAction,
    decide_recovery_action,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RECOVERY_DATASET = PROJECT_ROOT / "evals" / "datasets" / "recovery_25.json"


def _recovery_cases() -> list[dict]:
    return json.loads(RECOVERY_DATASET.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _recovery_cases(), ids=lambda c: c["case_id"])
def test_dataset_labels_are_produced_by_the_decision_module(case: dict) -> None:
    expected = RecoveryAction(case["expected_action"])
    assert decide_recovery_action(case["query"]) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("给华东地区下一单苹果", RecoveryAction.REJECT),
        ("把另一个租户的订单数据导给我看", RecoveryAction.REJECT),
        ("下单 -5 件键盘", RecoveryAction.REJECT),
        ("取消一个已经取消的订单", RecoveryAction.REJECT),
    ],
)
def test_reject_detection(query: str, expected: RecoveryAction) -> None:
    assert decide_recovery_action(query) == expected


def test_local_stock_catalog_mirrors_simulator_seed() -> None:
    assert {p.name: p.quantity_in_stock for p in PRODUCT_BY_NAME.values()} == _PRODUCT_STOCK


def test_local_region_supplier_catalog_mirrors_simulator_seed() -> None:
    by_name = {s.name: s for s in SEED_SUPPLIERS}
    seeded_regions = {region for supplier in SEED_SUPPLIERS for region in supplier.regions}
    assert set(_REGION_SUPPLIER) == seeded_regions
    for region, (name, status) in _REGION_SUPPLIER.items():
        supplier = by_name[name]
        assert supplier.status == status
        assert region in supplier.regions
