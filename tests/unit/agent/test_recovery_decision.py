"""Unit tests for agent/recovery_decision.py — deterministic recovery action.

``decide_recovery_action`` maps a natural-language query to one of four recovery
actions (ask_missing / confirm_conflict / retry / reject) using the same
deterministic, offline rules the agent runtime will reuse (docs/06 §7). The
datasets/recovery_25.json labels are the ground-truth contract: every query must
trigger its labeled action.

Local domain catalogs (_PRODUCT_STOCK / _REGION_SUPPLIER) mirror the ERP
Simulator seed data (apps/erp_simulator/data); the mirror tests guard against
drift. The delivery-region gate reads the runtime vocabulary loader
(manifest.yaml seed + approved vocabulary_terms) instead of a local copy, so a
region approved into the catalog stops being rejected. No LLM, no DB, no
network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from apps.erp_simulator.data.products import PRODUCT_BY_NAME
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS
from erp_copilot.agent.recovery_decision import (
    _PRODUCT_STOCK,
    _REGION_SUPPLIER,
    RecoveryAction,
    decide_recovery_action,
)
from erp_copilot.vocabulary import loader

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


@pytest.fixture(autouse=True)
def _reset_vocab_cache():
    """decide_recovery_action reads the process-global vocabulary cache; leave it clean."""
    yield
    loader.invalidate()


def _raw_manifest() -> dict:
    with open(loader._MANIFEST_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestCatalogDrivenRegionGate:
    """The region gate reads the merged loader catalog — approving a region lifts rejection."""

    def test_unapproved_region_is_rejected(self) -> None:
        assert decide_recovery_action("给苏州地区下一单苹果") == RecoveryAction.REJECT

    def test_approved_region_stops_being_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _raw_manifest()
        raw["supplier_region_enum"].append("苏州")
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(loader, "_MANIFEST_PATH", manifest)
        loader.invalidate()
        # 苏州 now valid, but quantity/unit absent -> graceful ask_missing.
        assert decide_recovery_action("给苏州地区下一单苹果") == RecoveryAction.ASK_MISSING
