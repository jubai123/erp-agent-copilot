"""Test the L1 skill matcher — deterministic intent → skill mapping.

The matcher MUST be deterministic (same input → same output every time),
never use vector search, and return exactly the skills mapped to each
(domain, action) pair.  Unknown intents return an empty list (no crash).
"""

from __future__ import annotations

import pytest

from erp_copilot.retrieval.skill_matcher import match_skills

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
    def test_returns_at_least_one_skill(self, domain: str, action: str) -> None:
        skills = match_skills(domain, action)
        assert isinstance(skills, list)
        assert len(skills) >= 1, f"Intent ({domain}, {action}) should match at least 1 skill"

    @pytest.mark.parametrize("domain,action", KNOWN_INTENTS)
    def test_each_skill_has_required_fields(self, domain: str, action: str) -> None:
        skills = match_skills(domain, action)
        for skill in skills:
            assert "skill_id" in skill, f"Missing skill_id in {skill}"
            assert "content" in skill, f"Missing content in {skill}"
            assert isinstance(skill["content"], str)
            assert len(skill["content"]) > 0

    @pytest.mark.parametrize("domain,action", KNOWN_INTENTS)
    def test_skills_are_unique(self, domain: str, action: str) -> None:
        skills = match_skills(domain, action)
        ids = [s["skill_id"] for s in skills]
        assert len(ids) == len(set(ids)), f"Duplicate skill_ids: {ids}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        first = match_skills("order", "create")
        second = match_skills("order", "create")
        assert first == second

    def test_different_intents_different_outputs(self) -> None:
        product_skills = match_skills("product", "query")
        order_skills = match_skills("order", "create")
        assert product_skills != order_skills


# ---------------------------------------------------------------------------
# Unknown intents — no crash
# ---------------------------------------------------------------------------


class TestUnknownIntents:
    def test_unknown_domain_returns_empty(self) -> None:
        skills = match_skills("nonexistent", "query")
        assert skills == []

    def test_unknown_action_returns_empty(self) -> None:
        skills = match_skills("product", "nonexistent")
        assert skills == []

    def test_both_unknown_returns_empty(self) -> None:
        skills = match_skills("foo", "bar")
        assert skills == []


# ---------------------------------------------------------------------------
# Content checks — L1 knowledge must NOT enter L2 retrieval
# ---------------------------------------------------------------------------


class TestL1Content:
    def test_order_state_machine_skill_exists(self) -> None:
        skills = match_skills("order", "update_status")
        state_machine = next((s for s in skills if s["skill_id"] == "order-state-machine"), None)
        assert state_machine is not None, "order-state-machine skill missing"
        assert "CREATED" in state_machine["content"]
        assert "CANCELLED" in state_machine["content"]

    def test_region_constraint_skill_exists(self) -> None:
        skills = match_skills("supplier", "query")
        region_skill = next((s for s in skills if s["skill_id"] == "region-enum-constraint"), None)
        assert region_skill is not None, "region-enum-constraint skill missing"
        # Should mention concrete region names, not just generic prose
        assert "上海" in region_skill["content"]

    def test_approval_policy_for_write_intents(self) -> None:
        for intent in [("order", "create"), ("order", "cancel"), ("order", "update_status")]:
            skills = match_skills(*intent)
            has_approval = any(s["skill_id"] == "approval-policy" for s in skills)
            assert has_approval, (
                f"Write intent ({intent[0]}, {intent[1]}) must include approval-policy"
            )

    def test_idempotency_for_order_create(self) -> None:
        skills = match_skills("order", "create")
        has_idem = any(s["skill_id"] == "idempotency-rule" for s in skills)
        assert has_idem, "order:create must include idempotency-rule"

    def test_stock_check_for_order_create(self) -> None:
        skills = match_skills("order", "create")
        has_stock = any(s["skill_id"] == "stock-check-rule" for s in skills)
        assert has_stock, "order:create must include stock-check-rule"
