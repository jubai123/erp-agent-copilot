"""Test the L1 rule matcher — deterministic intent → rule mapping.

The matcher MUST be deterministic (same input → same output every time),
never use vector search, and return exactly the rules mapped to each
(domain, action) pair.  Unknown intents return an empty list (no crash).
"""

from __future__ import annotations

import pytest

from erp_copilot.retrieval.rule_matcher import match_rules

# ---------------------------------------------------------------------------
# Known intent → non-empty result
# ---------------------------------------------------------------------------

KNOWN_INTENTS = [
    ("product", "query"),
    ("product", "check_stock"),
    ("supplier", "query"),
    ("order", "create"),
    ("order", "query"),
    ("order", "cancel"),
    ("order", "update_status"),
]


class TestKnownIntents:
    @pytest.mark.parametrize("domain,action", KNOWN_INTENTS)
    def test_returns_at_least_one_rule(self, domain: str, action: str) -> None:
        rules = match_rules(domain, action)
        assert isinstance(rules, list)
        assert len(rules) >= 1, f"Intent ({domain}, {action}) should match at least 1 rule"

    @pytest.mark.parametrize("domain,action", KNOWN_INTENTS)
    def test_each_rule_has_required_fields(self, domain: str, action: str) -> None:
        rules = match_rules(domain, action)
        for rule in rules:
            assert "rule_id" in rule, f"Missing rule_id in {rule}"
            assert "content" in rule, f"Missing content in {rule}"
            assert isinstance(rule["content"], str)
            assert len(rule["content"]) > 0

    @pytest.mark.parametrize("domain,action", KNOWN_INTENTS)
    def test_rules_are_unique(self, domain: str, action: str) -> None:
        rules = match_rules(domain, action)
        ids = [r["rule_id"] for r in rules]
        assert len(ids) == len(set(ids)), f"Duplicate rule_ids: {ids}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        first = match_rules("order", "create")
        second = match_rules("order", "create")
        assert first == second

    def test_different_intents_different_outputs(self) -> None:
        product_rules = match_rules("product", "query")
        order_rules = match_rules("order", "create")
        assert product_rules != order_rules


# ---------------------------------------------------------------------------
# Unknown intents — no crash
# ---------------------------------------------------------------------------


class TestUnknownIntents:
    def test_unknown_domain_returns_empty(self) -> None:
        rules = match_rules("nonexistent", "query")
        assert rules == []

    def test_unknown_action_returns_empty(self) -> None:
        rules = match_rules("product", "nonexistent")
        assert rules == []

    def test_both_unknown_returns_empty(self) -> None:
        rules = match_rules("foo", "bar")
        assert rules == []


# ---------------------------------------------------------------------------
# Content checks — L1 knowledge must NOT enter L2 retrieval
# ---------------------------------------------------------------------------


class TestL1Content:
    def test_order_state_machine_rule_exists(self) -> None:
        rules = match_rules("order", "update_status")
        state_machine = next((r for r in rules if r["rule_id"] == "order-state-machine"), None)
        assert state_machine is not None, "order-state-machine rule missing"
        assert "CREATED" in state_machine["content"]
        assert "CANCELLED" in state_machine["content"]

    def test_region_constraint_rule_exists(self) -> None:
        rules = match_rules("supplier", "query")
        region_rule = next((r for r in rules if r["rule_id"] == "region-enum-constraint"), None)
        assert region_rule is not None, "region-enum-constraint rule missing"
        # Should mention concrete region names, not just generic prose
        assert "上海" in region_rule["content"]

    def test_approval_policy_for_write_intents(self) -> None:
        for intent in [("order", "create"), ("order", "cancel"), ("order", "update_status")]:
            rules = match_rules(*intent)
            has_approval = any(r["rule_id"] == "approval-policy" for r in rules)
            assert has_approval, (
                f"Write intent ({intent[0]}, {intent[1]}) must include approval-policy"
            )

    def test_idempotency_for_order_create(self) -> None:
        rules = match_rules("order", "create")
        has_idem = any(r["rule_id"] == "idempotency-rule" for r in rules)
        assert has_idem, "order:create must include idempotency-rule"

    def test_stock_check_for_order_create(self) -> None:
        rules = match_rules("order", "create")
        has_stock = any(r["rule_id"] == "stock-check-rule" for r in rules)
        assert has_stock, "order:create must include stock-check-rule"
