"""Drift guards for the runtime vocabulary loader — Phase A of the LLM-vocabulary task.

datasets/knowledge/manifest.yaml is the declared authority for product/region/unit
catalogs, but nothing loaded it at runtime before this loader. The hardcoded
mirrors (classify_intent, routing, recovery_decision) drifted from it silently.
These tests pin the loader to the manifest AND pin the intentionally-hardcoded
structural catalogs (_STATUS_MAP, _ORDER_STATE_GRAPH) to the manifest's lifecycle
enum, so a future manifest change fails loudly instead of silently diverging.
"""

from __future__ import annotations

import pytest
import yaml

from erp_copilot.agent import recovery_decision
from erp_copilot.agent.nodes import classify_intent
from erp_copilot.vocabulary import loader
from erp_copilot.vocabulary.loader import (
    VocabularyTermLike,
    get_catalog,
    get_product_association_pattern,
    get_quantity_pattern,
    load_manifest_catalog,
    merge_catalog,
)


@pytest.fixture(autouse=True)
def _reset_vocab_cache():
    """The module cache is process-global; every test leaves it clean."""
    yield
    loader.invalidate()


def _raw_manifest() -> dict:
    with open(loader._MANIFEST_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _lifecycle_states() -> set[str]:
    lc = load_manifest_catalog().order_status_lifecycle
    return {
        lc.initial,
        *lc.terminal,
        *lc.transitions.keys(),
        *(target for targets in lc.transitions.values() for target in targets),
    }


def test_catalog_regions_equal_manifest_supplier_region_enum():
    assert get_catalog().regions == tuple(_raw_manifest()["supplier_region_enum"])


def test_catalog_products_equal_manifest_product_names():
    expected = tuple(p["name"] for p in _raw_manifest()["products"])
    assert get_catalog().products == expected


def test_catalog_units_equal_manifest_product_unit_enum():
    assert get_catalog().units == tuple(_raw_manifest()["product_unit_enum"])


def test_status_map_targets_subset_of_manifest_lifecycle():
    # _STATUS_MAP maps Chinese order verbs -> ERP status tokens and stays
    # hardcoded (manifest only carries the state enum, not the verbs); its
    # targets must never name a state the manifest does not know.
    targets = {target for _, target in classify_intent._STATUS_MAP}
    assert targets <= _lifecycle_states()


def test_recovery_decision_state_graph_known_states_match_manifest():
    known = set(recovery_decision._ORDER_STATE_GRAPH)
    assert known <= _lifecycle_states()
    # Intentional omission: manifest allows CREATED/CONFIRMED/SHIPPED -> CANCELLED,
    # but decide_recovery_action treats re-cancelling as REJECT via
    # _TERMINAL_RECANCEL_TOKENS. Pinned here so a manifest change surfaces rather
    # than silently changing recovery behaviour.
    assert "CANCELLED" not in recovery_decision._ORDER_STATE_GRAPH


def test_merge_catalog_appends_and_dedupes():
    seed = load_manifest_catalog()
    merged = merge_catalog(
        seed,
        [
            VocabularyTermLike("region", "苏州"),
            VocabularyTermLike("region", "上海"),  # already in seed -> deduped
            VocabularyTermLike("product", "榴莲"),
            VocabularyTermLike("unit", "kg"),  # case-sensitive: distinct from seed "KG"
        ],
    )
    assert merged.regions == (*seed.regions, "苏州")
    assert merged.products == (*seed.products, "榴莲")
    assert merged.units == (*seed.units, "kg")
    assert merged.regions.count("上海") == 1


def test_merge_catalog_ignores_unknown_vocab_type():
    seed = load_manifest_catalog()
    merged = merge_catalog(seed, [VocabularyTermLike("status", "DELIVERED")])
    assert merged.regions == seed.regions
    assert merged.products == seed.products
    assert merged.units == seed.units


def test_get_quantity_pattern_compiles_and_matches():
    pattern = get_quantity_pattern(("KG", "件"))
    assert pattern.search("10 KG") is not None
    assert pattern.search("5件") is not None
    assert pattern.search("20") is not None  # unit is optional


def test_get_quantity_pattern_recompiles_on_catalog_change():
    pattern_a = get_quantity_pattern(("KG",))
    pattern_b = get_quantity_pattern(("KG", "件"))
    assert pattern_a.pattern != pattern_b.pattern
    assert "件" in pattern_b.pattern
    assert "件" not in pattern_a.pattern


def test_get_product_association_pattern_matches_known_product():
    pattern = get_product_association_pattern(("苹果", "香蕉"))
    assert pattern.search("苹果的供应商") is not None
    assert pattern.search("正常状态的供应商") is None
