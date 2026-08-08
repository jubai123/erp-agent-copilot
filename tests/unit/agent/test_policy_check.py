"""Unit tests for the policy_check node — task 4.8.

Decides each plan step as ALLOW / DENY / REQUIRE_APPROVAL by combining the
user's scopes (resolved from user_id by an injected callable) with the step's
required_scope and risk level (docs/03 §4). Scope gate runs first — a step
whose required_scope the user does not hold is DENYed regardless of risk, so
approval can never grant scope. READ steps with no explicit requires_approval
flag are ALLOWed; everything else is REQUIRE_APPROVAL.

The acceptance table (task 4.8) maps onto this: READ→ALLOW; WRITE + 无 Scope→
DENY; WRITE + 有 Scope→REQUIRE_APPROVAL; ADMIN→REQUIRE_APPROVAL (admin tools
carry a required_scope, so an unscoped user is DENYed — the stricter reading
the table leaves implicit).
"""

from __future__ import annotations

from erp_copilot.agent.nodes.policy_check import (
    PolicyDecision,
    build_policy_check_node,
    decide_step,
)
from erp_copilot.agent.state import AgentState, Plan, PlanStep
from erp_copilot.domain.enums import ToolRiskLevel


def _step(
    step_id: str = "s1",
    tool_name: str = "getProductById",
    **overrides: object,
) -> PlanStep:
    data: dict[str, object] = {
        "step_id": step_id,
        "tool_name": tool_name,
        "risk_level": ToolRiskLevel.READ,
    }
    data.update(overrides)
    return PlanStep(**data)


class TestDecideStep:
    def test_read_step_allowed_without_scope(self) -> None:
        assert decide_step(_step(), frozenset()) == PolicyDecision.ALLOW

    def test_write_step_with_scope_requires_approval(self) -> None:
        step = _step(
            tool_name="updateOrderStatus",
            risk_level=ToolRiskLevel.WRITE,
            required_scope="order:write",
        )
        assert decide_step(step, {"order:write"}) == PolicyDecision.REQUIRE_APPROVAL

    def test_write_step_without_scope_denied(self) -> None:
        step = _step(
            tool_name="updateOrderStatus",
            risk_level=ToolRiskLevel.WRITE,
            required_scope="order:write",
        )
        assert decide_step(step, set()) == PolicyDecision.DENY

    def test_write_step_without_required_scope_requires_approval(self) -> None:
        step = _step(
            tool_name="updateOrderStatus",
            risk_level=ToolRiskLevel.WRITE,
        )
        assert decide_step(step, set()) == PolicyDecision.REQUIRE_APPROVAL

    def test_dangerous_step_with_scope_requires_approval(self) -> None:
        step = _step(
            tool_name="deleteOrder",
            risk_level=ToolRiskLevel.DANGEROUS,
            required_scope="admin:write",
        )
        assert decide_step(step, {"admin:write"}) == PolicyDecision.REQUIRE_APPROVAL

    def test_dangerous_step_without_scope_denied(self) -> None:
        step = _step(
            tool_name="deleteOrder",
            risk_level=ToolRiskLevel.DANGEROUS,
            required_scope="admin:write",
        )
        assert decide_step(step, set()) == PolicyDecision.DENY

    def test_read_step_without_required_scope_denied(self) -> None:
        step = _step(required_scope="product:read")
        assert decide_step(step, set()) == PolicyDecision.DENY

    def test_read_step_with_requires_approval_flag_requires_approval(self) -> None:
        step = _step(requires_approval=True)
        assert decide_step(step, frozenset()) == PolicyDecision.REQUIRE_APPROVAL


class TestBuildPolicyCheckNode:
    def test_node_builds_decision_map(self) -> None:
        node = build_policy_check_node(get_scopes=lambda _tenant, _user: {"order:write"})
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            user_id="u1",
            plan=Plan(
                steps=[
                    _step(step_id="r"),
                    _step(
                        step_id="w",
                        tool_name="updateOrderStatus",
                        risk_level=ToolRiskLevel.WRITE,
                        required_scope="order:write",
                    ),
                ]
            ),
        )
        updates = node(state)
        assert updates["policy_decisions"]["r"] == PolicyDecision.ALLOW
        assert updates["policy_decisions"]["w"] == PolicyDecision.REQUIRE_APPROVAL
        assert "errors" not in updates

    def test_node_appends_policy_denied_error(self) -> None:
        node = build_policy_check_node(get_scopes=lambda _tenant, _user: set())
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            user_id="u1",
            plan=Plan(
                steps=[
                    _step(
                        step_id="w",
                        tool_name="updateOrderStatus",
                        risk_level=ToolRiskLevel.WRITE,
                        required_scope="order:write",
                    )
                ]
            ),
        )
        updates = node(state)
        assert updates["policy_decisions"]["w"] == PolicyDecision.DENY
        assert [e.code for e in updates["errors"]] == ["POLICY_DENIED"]

    def test_node_preserves_existing_errors(self) -> None:
        from erp_copilot.agent.state import StateError

        node = build_policy_check_node(get_scopes=lambda _tenant, _user: set())
        existing = StateError(code="EXISTING", message="x")
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            user_id="u1",
            errors=[existing],
            plan=Plan(
                steps=[
                    _step(
                        step_id="w",
                        tool_name="updateOrderStatus",
                        risk_level=ToolRiskLevel.WRITE,
                        required_scope="order:write",
                    )
                ]
            ),
        )
        updates = node(state)
        assert [e.code for e in updates["errors"]] == ["EXISTING", "POLICY_DENIED"]

    def test_node_none_plan_returns_empty_decisions(self) -> None:
        node = build_policy_check_node(get_scopes=lambda _tenant, _user: set())
        state = AgentState(run_id="r1", tenant_id="t1", query="q", plan=None)
        updates = node(state)
        assert updates["policy_decisions"] == {}
        assert "errors" not in updates

    def test_node_without_user_id_resolves_no_scopes(self) -> None:
        def resolver(_tenant: str, _user: str) -> set[str]:
            raise AssertionError("resolver must not run without user_id")

        node = build_policy_check_node(get_scopes=resolver)
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            user_id=None,
            plan=Plan(steps=[_step(required_scope="product:read")]),
        )
        updates = node(state)
        assert updates["policy_decisions"]["s1"] == PolicyDecision.DENY

    def test_get_scopes_receives_tenant_and_user(self) -> None:
        seen: list[tuple[str, str]] = []

        def resolver(tenant_id: str, user_id: str) -> frozenset[str]:
            seen.append((tenant_id, user_id))
            return frozenset({"product:read"})

        node = build_policy_check_node(get_scopes=resolver)
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            user_id="u42",
            plan=Plan(steps=[_step(required_scope="product:read")]),
        )
        node(state)
        assert seen == [("t1", "u42")]
