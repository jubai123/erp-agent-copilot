"""LangGraph state-graph skeleton — task 4.2.

Wires the nine Phase-4 nodes in the design-doc topology (docs/03 §4).
Nodes are stubs here; each receives a real module in its own task
(4.3-4.11). request_approval is deferred to Phase 5 (task 5.2) by
decision — the acceptance for task 4.2 is a 9-node graph.
"""

from __future__ import annotations

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


def retrieve_context(state: AgentState) -> dict[str, Any]:
    """TODO(task 4.4): L2 retrieval — mandatory node, LLM never skips it."""
    return {}


def build_plan(state: AgentState) -> dict[str, Any]:
    """TODO(task 4.6): LLM emits a Plan DAG from the constrained candidates."""
    return {}


def validate_plan(state: AgentState) -> dict[str, Any]:
    """TODO(task 4.7): reject invalid DAGs, compute topological order."""
    return {}


def policy_check(state: AgentState) -> dict[str, Any]:
    """TODO(task 4.8): ALLOW/DENY from scope; approval branch lands in Phase 5."""
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


def build_agent_graph() -> CompiledStateGraph:
    """Build and compile the agent state graph."""
    builder = StateGraph(AgentState)
    # Registered explicitly — langgraph's _Node protocol does not type-check
    # when node functions are passed through a dict iteration.
    builder.add_node("classify_intent", classify_intent_node)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("build_plan", build_plan)
    builder.add_node("validate_plan", validate_plan)
    builder.add_node("policy_check", policy_check)
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
