"""LangGraph state-graph skeleton — task 4.2.

Wires the nine Phase-4 nodes in the design-doc topology (docs/03 §4).
Nodes are stubs here; each receives a real module in its own task
(4.3-4.11). request_approval is deferred to Phase 5 (task 5.2) by
decision — the acceptance for task 4.2 is a 9-node graph.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from erp_copilot.agent.nodes.classify_intent import classify_intent_node
from erp_copilot.agent.state import AgentState, AgentStatus

# Node names in execution order (docs/03 §4, minus request_approval).
NODE_NAMES: tuple[str, ...] = (
    "classify_intent",
    "retrieve_context",
    "build_plan",
    "validate_plan",
    "policy_check",
    "execute_ready_steps",
    "verify_results",
    "recover_or_replan",
    "finalize",
)


# -- Node stubs (replaced by real modules in tasks 4.4-4.11) -------------------


def _retrieve_context_noop(state: AgentState) -> dict[str, Any]:
    """No-op retrieve_context used when no backend is injected.

    Keeps topology and smoke tests independent of a database; the app always
    injects the real node (build_retrieve_context_node) at startup.
    """
    return {}


def _build_plan_noop(state: AgentState) -> dict[str, Any]:
    """No-op build_plan used when no LLM is injected.

    Keeps topology and smoke tests free of an LLM dependency; the app always
    injects the real node (build_plan_node) at startup.
    """
    return {}


def _validate_plan_noop(state: AgentState) -> dict[str, Any]:
    """No-op validate_plan used when no tool schemas are injected.

    Keeps topology and smoke tests free of tool-schema wiring; the app always
    injects the real node (build_validate_plan_node) at startup.
    """
    return {}


def _policy_check_noop(state: AgentState) -> dict[str, Any]:
    """No-op policy_check used when no scope resolver is injected.

    Keeps topology and smoke tests free of a security-subsystem dependency;
    the app always injects the real node (build_policy_check_node) at startup.
    """
    return {}


def execute_ready_steps(state: AgentState) -> dict[str, Any]:
    """TODO(tasks 4.9-4.10): run ready steps — READ parallel, WRITE serial."""
    return {}


def verify_results(state: AgentState) -> dict[str, Any]:
    """TODO(task 4.11): match success_condition against the business goal."""
    return {"status": AgentStatus.SUCCEEDED}


def recover_or_replan(state: AgentState) -> dict[str, Any]:
    """TODO(task 4.11/5.8): decide retry vs replan vs give-up."""
    return {}


def finalize(state: AgentState) -> dict[str, Any]:
    """TODO: persist the Run and emit the final event."""
    return {}


# -- Routing ------------------------------------------------------------------


def _route_after_validate(state: AgentState) -> str:
    return "recover_or_replan" if state.errors else "policy_check"


def _route_after_policy(state: AgentState) -> str:
    # Phase 5 (task 5.2) adds a request_approval branch for REQUIRE_APPROVAL.
    return "execute_ready_steps"


def _route_after_verify(state: AgentState) -> str:
    return "recover_or_replan" if state.errors else "finalize"


def _route_after_recover(state: AgentState) -> str:
    # Retry/replan budget logic arrives with tasks 4.11/5.8; the edge mapping
    # in build_agent_graph still declares all three transitions as legal.
    return "finalize"


def build_agent_graph(
    retrieve_node: Callable[[AgentState], dict[str, Any]] | None = None,
    plan_node: Callable[[AgentState], dict[str, Any]] | None = None,
    validate_node: Callable[[AgentState], dict[str, Any]] | None = None,
    policy_node: Callable[[AgentState], dict[str, Any]] | None = None,
) -> CompiledStateGraph:
    """Build and compile the agent state graph.

    *retrieve_node* injects the real retrieve_context implementation (task
    4.4), *plan_node* the real build_plan implementation (task 4.6),
    *validate_node* the real validate_plan implementation (task 4.7) and
    *policy_node* the real policy_check implementation (task 4.8). When
    omitted, no-ops keep topology/smoke tests free of database, LLM,
    tool-schema and security-subsystem dependencies.
    """
    builder = StateGraph(AgentState)
    # Registered explicitly — langgraph's _Node protocol does not type-check
    # when node functions are passed through a dict iteration.
    builder.add_node("classify_intent", classify_intent_node)
    builder.add_node(
        "retrieve_context",
        retrieve_node if retrieve_node is not None else _retrieve_context_noop,  # type: ignore[arg-type]
    )
    builder.add_node(
        "build_plan",
        plan_node if plan_node is not None else _build_plan_noop,  # type: ignore[arg-type]
    )
    builder.add_node(
        "validate_plan",
        validate_node if validate_node is not None else _validate_plan_noop,  # type: ignore[arg-type]
    )
    builder.add_node(
        "policy_check",
        policy_node if policy_node is not None else _policy_check_noop,  # type: ignore[arg-type]
    )
    builder.add_node("execute_ready_steps", execute_ready_steps)
    builder.add_node("verify_results", verify_results)
    builder.add_node("recover_or_replan", recover_or_replan)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "classify_intent")
    builder.add_edge("classify_intent", "retrieve_context")
    builder.add_edge("retrieve_context", "build_plan")
    builder.add_edge("build_plan", "validate_plan")
    builder.add_conditional_edges(
        "validate_plan",
        _route_after_validate,
        {"policy_check": "policy_check", "recover_or_replan": "recover_or_replan"},
    )
    builder.add_conditional_edges(
        "policy_check",
        _route_after_policy,
        {"execute_ready_steps": "execute_ready_steps", "finalize": "finalize"},
    )
    builder.add_edge("execute_ready_steps", "verify_results")
    builder.add_conditional_edges(
        "verify_results",
        _route_after_verify,
        {"finalize": "finalize", "recover_or_replan": "recover_or_replan"},
    )
    builder.add_conditional_edges(
        "recover_or_replan",
        _route_after_recover,
        {
            "execute_ready_steps": "execute_ready_steps",
            "build_plan": "build_plan",
            "finalize": "finalize",
        },
    )
    builder.add_edge("finalize", END)

    return builder.compile()
