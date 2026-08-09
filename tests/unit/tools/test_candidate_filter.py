"""Unit tests for tools/candidate_filter.py — ADR decision 5.

First-level deterministic intent→tool filtering (DOMAIN_TOOL_MAP) with a
reserved second-level vector-rerank trigger (should_use_tool_retrieval).
The intent keys MUST stay in sync with intent_skill_map.yaml so L1 skill
injection and tool candidate filtering share one intent taxonomy.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from erp_copilot.tools.candidate_filter import (
    DOMAIN_TOOL_MAP,
    V6_TOOL_NAMES,
    filter_candidates,
    should_use_tool_retrieval,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INTENT_MAP_PATH = PROJECT_ROOT / "datasets" / "knowledge" / "skills" / "intent_skill_map.yaml"


def _load_intent_keys() -> set[tuple[str, str]]:
    raw = yaml.safe_load(INTENT_MAP_PATH.read_text(encoding="utf-8"))
    return {(e["domain"], e["action"]) for e in raw["intents"]}


class TestDomainToolMap:
    def test_intent_keys_match_skill_matcher_map(self) -> None:
        """L1 injection and tool filtering must share the intent taxonomy."""
        assert set(DOMAIN_TOOL_MAP) == _load_intent_keys()

    def test_all_v6_tools_referenced(self) -> None:
        mapped = {t for tools in DOMAIN_TOOL_MAP.values() for t in tools}
        assert mapped == set(V6_TOOL_NAMES)

    def test_candidates_are_3_to_8_for_main_intents(self) -> None:
        """Main write intents get 3-8 candidates (design doc: 3-5)."""
        for intent in [("order", "create"), ("product", "query"), ("order", "cancel")]:
            candidates = DOMAIN_TOOL_MAP[intent]
            assert 3 <= len(candidates) <= 8, f"{intent}: {len(candidates)} candidates"

    def test_supplier_intents_have_all_v6_supplier_tools(self) -> None:
        """V6 registers only 2 supplier tools — both must be candidates."""
        assert set(DOMAIN_TOOL_MAP[("supplier", "query")]) == {
            "querySuppliersByDeliveryRegion",
            "getSupplierByStatus",
        }

    def test_mapping_has_no_duplicates(self) -> None:
        for intent, tools in DOMAIN_TOOL_MAP.items():
            assert len(tools) == len(set(tools)), f"{intent} has duplicate tools"

    def test_v6_tool_set_matches_design_doc(self) -> None:
        assert (
            frozenset(
                {
                    "getProductByName",
                    "getProductById",
                    "getProductSubstitutesByName",
                    "querySuppliersByDeliveryRegion",
                    "getSupplierByStatus",
                    "getOrderByOrderId",
                    "createOrder",
                    "updateOrderStatus",
                    "cancelOrder",
                }
            )
            == V6_TOOL_NAMES
        )


class TestFilterCandidates:
    def test_returns_mapping_subset(self) -> None:
        candidates = filter_candidates("order", "create")
        assert candidates == [
            "getProductByName",
            "querySuppliersByDeliveryRegion",
            "getSupplierByStatus",
            "createOrder",
        ]

    def test_restricted_by_available_tools(self) -> None:
        available = ["getProductByName", "createOrder"]
        candidates = filter_candidates("order", "create", available_tools=available)
        assert candidates == ["getProductByName", "createOrder"]

    def test_unknown_intent_returns_empty(self) -> None:
        assert filter_candidates("order", "explode") == []
        assert filter_candidates("unknown", "query") == []

    def test_default_available_is_v6_registered(self) -> None:
        candidates = filter_candidates("order", "create")
        assert all(c in V6_TOOL_NAMES for c in candidates)


class TestShouldUseToolRetrieval:
    def test_below_threshold_disabled(self) -> None:
        assert should_use_tool_retrieval(["a", "b", "c", "d"]) is False

    def test_at_threshold_disabled(self) -> None:
        """'超过阈值' means strictly greater — at threshold still no rerank."""
        assert should_use_tool_retrieval(["a", "b", "c", "d", "e"]) is False

    def test_above_threshold_enabled(self) -> None:
        assert should_use_tool_retrieval(["a", "b", "c", "d", "e", "f"]) is True

    def test_custom_threshold(self) -> None:
        assert should_use_tool_retrieval(["a", "b"], threshold=1) is True
        assert should_use_tool_retrieval(["a"], threshold=1) is False

    def test_empty_candidates_disabled(self) -> None:
        assert should_use_tool_retrieval([]) is False

    def test_current_v6_intents_never_trigger_rerank(self) -> None:
        """With 9 tools, no intent exceeds the threshold — rerank stays off."""
        for intent, tools in DOMAIN_TOOL_MAP.items():
            assert should_use_tool_retrieval(tools) is False, (
                f"{intent} unexpectedly triggers tool retrieval"
            )
