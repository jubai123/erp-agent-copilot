"""Deterministic policy gate node — task 4.8.

Decides each plan step as ALLOW / DENY / REQUIRE_APPROVAL by combining the
user's scopes with the step's required_scope and risk level (docs/03 §4
policy_check). The scope gate runs first — a step whose required_scope the
user does not hold is DENYed regardless of risk, so a later approval can never
grant scope (approval gates execution, it does not grant permission). READ
steps are ALLOWed; WRITE/DANGEROUS steps and anything explicitly flagged
requires_approval need a human.

Scopes are resolved from (tenant_id, user_id) by an injected callable so the
node is unit-testable without a security subsystem; the app injects the RBAC
resolver at startup. Passing the tenant keeps scopes tenant-scoped — the same
user may hold different scopes in different tenants. user_id=None resolves to
no scopes — the safe default.
"""

from __future__ import annotations

from collections.abc import Callable, Set
from typing import Any

from erp_copilot.agent.state import AgentState, PlanStep, PolicyDecision, StateError
from erp_copilot.domain.enums import ToolRiskLevel


def decide_step(step: PlanStep, scopes: Set[str]) -> PolicyDecision:
    """One-step policy decision: scope gate first, then risk/approval gate."""
    if step.required_scope and step.required_scope not in scopes:
        return PolicyDecision.DENY
    if step.risk_level == ToolRiskLevel.READ and not step.requires_approval:
        return PolicyDecision.ALLOW
    return PolicyDecision.REQUIRE_APPROVAL


def build_policy_check_node(
    *,
    get_scopes: Callable[[str, str], Set[str]],
) -> Callable[[AgentState], dict[str, Any]]:
    """Build the policy_check LangGraph node with an injected scope resolver.

    *get_scopes* receives (tenant_id, user_id) and returns that user's scopes
    within that tenant. The node writes a step_id → PolicyDecision map into
    state.policy_decisions and appends POLICY_DENIED errors for denied steps so
    the graph can route denied plans back before execution.
    """

    def policy_check_node(state: AgentState) -> dict[str, Any]:
        decisions: dict[str, PolicyDecision] = {}
        errors: list[StateError] = []
        if state.plan is not None:
            scopes: Set[str] = (
                set(get_scopes(state.tenant_id, state.user_id)) if state.user_id else set()
            )
            for step in state.plan.steps:
                decision = decide_step(step, scopes)
                decisions[step.step_id] = decision
                if decision is PolicyDecision.DENY:
                    errors.append(
                        StateError(
                            code="POLICY_DENIED",
                            message=(
                                f"step {step.step_id} 需要权限 {step.required_scope}，"
                                "但用户不具备该 Scope"
                            ),
                            step_id=step.step_id,
                        )
                    )
        updates: dict[str, Any] = {"policy_decisions": decisions}
        if errors:
            updates["errors"] = [*state.errors, *errors]
        return updates

    return policy_check_node
