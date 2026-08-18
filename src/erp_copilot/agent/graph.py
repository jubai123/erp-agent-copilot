"""LangGraph state-graph assembly — task 4.2.

Wires the Phase-4/5 nodes in the design-doc topology (docs/03 §4) plus the
check_deadline gate (task 4.15) and the recover_or_replan sink (tasks 4.11/5.8).
Real node implementations are injected by the caller (worker, app); when
omitted, no-op stand-ins keep topology/smoke tests free of their dependencies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from erp_copilot.agent.nodes.classify_intent import classify_intent_node
from erp_copilot.agent.nodes.recover_or_replan import recover_or_replan
from erp_copilot.agent.nodes.request_approval import (
    pending_approval_step_ids,
    request_approval_node,
)
from erp_copilot.agent.routing import route_query_layer
from erp_copilot.agent.state import AgentState, AgentStatus, StateError
from erp_copilot.memory.checkpoint import CheckpointSaver, checkpointed

# Node names in execution order (docs/03 §4). check_deadline runs first so a
# run past its deadline (task 4.15) terminates before any work begins.
NODE_NAMES: tuple[str, ...] = (
    "check_deadline",
    "classify_intent",
    "retrieve_context",
    "build_plan",
    "build_plan_llm",
    "validate_plan",
    "policy_check",
    "request_approval",
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


def _execute_steps_noop(state: AgentState) -> dict[str, Any]:
    """No-op execute_ready_steps used when no executor is injected.

    Keeps topology and smoke tests free of a tool-executor dependency; the app
    always injects the real node (build_execute_steps_node) at startup.
    """
    return {}


def _verify_results_noop(state: AgentState) -> dict[str, Any]:
    """No-op verify_results used when no verification logic is injected.

    Keeps topology and smoke tests free of success-condition evaluation; the
    app always injects the real node (build_verify_results_node) at startup.
    """
    return {"status": AgentStatus.SUCCEEDED}


def _llm_plan_noop(state: AgentState) -> dict[str, Any]:
    """Honest fail for tier2/3 queries when no LLM plan node is injected.

    The three-layer funnel's boundary is enforced here: a query the
    deterministic layer cannot faithfully serve (route_query_layer != tier1)
    must not be grabbed by it. With no injected LLM planner the graph reports
    ROUTED_TIER23_NO_LLM and the run fails honestly instead of producing a
    wrong plan; the worker injects the real LLM node (build_plan_node) when
    one is configured.
    """
    return {
        "plan": None,
        "errors": [
            *state.errors,
            StateError(
                code="ROUTED_TIER23_NO_LLM",
                message="该查询需要 Tier2/Tier3 LLM 规划，但 graph 未注入 LLM 规划节点",
            ),
        ],
    }


def check_deadline(state: AgentState) -> dict[str, Any]:
    """Terminate a run whose deadline has passed (task 4.15).

    With no deadline set the node is a no-op, so runs that never opt in behave
    exactly as before. A run past its deadline transitions to EXPIRED; the
    worker maps EXPIRED onto a FAILED Run with a DEADLINE_EXCEEDED code. The
    node runs on every invocation, so a run paused in WAITING_APPROVAL that
    passes its deadline expires on the next resume.
    """
    if state.deadline_at is None or datetime.now(UTC) <= state.deadline_at:
        return {}
    return {
        "status": AgentStatus.EXPIRED,
        "errors": [
            *state.errors,
            StateError(code="DEADLINE_EXCEEDED", message="run exceeded its deadline"),
        ],
    }


def finalize(state: AgentState) -> dict[str, Any]:
    """TODO: persist the Run and emit the final event."""
    return {}


# -- Routing ------------------------------------------------------------------


def _route_after_deadline(state: AgentState) -> str:
    return "finalize" if state.status == AgentStatus.EXPIRED else "classify_intent"


def _route_after_validate(state: AgentState) -> str:
    return "recover_or_replan" if state.errors else "policy_check"


def _route_after_approval(state: AgentState) -> str:
    # A step awaiting approval pauses the run here — the graph ends and the
    # checkpoint keeps the run in WAITING_APPROVAL until a human decides. Once
    # every REQUIRE_APPROVAL step has a decided record, the run proceeds.
    # Deciding against the *current* plan (not state.status, which classify
    # resets to PLANNING on a resumed run) also drops stale PENDING records
    # left behind by an earlier plan.
    return "waiting" if pending_approval_step_ids(state) else "proceed"


def _route_after_verify(state: AgentState) -> str:
    return "recover_or_replan" if state.errors else "finalize"


def _route_to_planner(state: AgentState) -> str:
    # The three-layer funnel's plan-node choice: tier1 serves the deterministic
    # planner (fast, offline), tier2/tier3 route to the injected LLM planner.
    # route_query_layer is pure and deterministic, so the forward edge and the
    # replan edge (recover_or_replan PLANNING -> here) agree.
    # A resumed run already carries a validated plan approved by a human on a
    # previous pass; re-planning it — especially via the LLM, whose step_ids are
    # not positional — would orphan the approval records (they reference the old
    # step_ids) and could swap in a plan nobody approved. Skip straight to
    # re-validating the existing plan. recover_or_replan's PLANNING branch sets
    # plan to None before routing back, so a genuine re-plan still re-enters the
    # planner.
    if state.plan is not None:
        return "validate_plan"
    return "build_plan" if route_query_layer(state.query) == "tier1" else "build_plan_llm"


def _route_after_recover(state: AgentState) -> str:
    # recover_or_replan sets the next status: EXECUTING -> retry the failed
    # steps, PLANNING -> regenerate the plan (re-entering the layer router);
    # anything else (FAILED after give up, or a no-op status) ends at finalize.
    if state.status == AgentStatus.EXECUTING:
        return "execute_ready_steps"
    if state.status == AgentStatus.PLANNING:
        return _route_to_planner(state)
    return "finalize"


def build_agent_graph(
    retrieve_node: Callable[[AgentState], dict[str, Any]] | None = None,
    plan_node: Callable[[AgentState], dict[str, Any]] | None = None,
    llm_plan_node: Callable[[AgentState], dict[str, Any]] | None = None,
    validate_node: Callable[[AgentState], dict[str, Any]] | None = None,
    policy_node: Callable[[AgentState], dict[str, Any]] | None = None,
    execute_node: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
    verify_node: Callable[[AgentState], dict[str, Any]] | None = None,
    checkpoint_saver: CheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Build and compile the agent state graph.

    *retrieve_node* injects the real retrieve_context implementation (task
    4.4), *plan_node* the real build_plan implementation (task 4.6),
    *llm_plan_node* the real Tier2/Tier3 LLM build_plan implementation (the
    three-layer funnel: the graph routes a query to *plan_node* when
    route_query_layer says tier1, else to *llm_plan_node*),
    *validate_node* the real validate_plan implementation (task 4.7),
    *policy_node* the real policy_check implementation (task 4.8),
    *execute_node* the real execute_ready_steps implementation (task 4.9) and
    *verify_node* the real verify_results implementation (task 4.11). When
    omitted, no-ops keep topology/smoke tests free of database, LLM,
    tool-schema, security-subsystem, executor and success-condition
    dependencies. An omitted *llm_plan_node* reports ROUTED_TIER23_NO_LLM for
    tier2/3 queries — the funnel refuses to overgrab rather than guess.

    *checkpoint_saver* (task 4.12) wraps every node so its post-node state is
    persisted before the graph advances; omitted in tests, injected by the
    worker at startup.
    """
    builder = StateGraph(AgentState)
    nodes: list[tuple[str, Callable[..., Any]]] = [
        ("check_deadline", check_deadline),
        ("classify_intent", classify_intent_node),
        (
            "retrieve_context",
            retrieve_node if retrieve_node is not None else _retrieve_context_noop,
        ),
        ("build_plan", plan_node if plan_node is not None else _build_plan_noop),
        (
            "build_plan_llm",
            llm_plan_node if llm_plan_node is not None else _llm_plan_noop,
        ),
        ("validate_plan", validate_node if validate_node is not None else _validate_plan_noop),
        ("policy_check", policy_node if policy_node is not None else _policy_check_noop),
        ("request_approval", request_approval_node),
        (
            "execute_ready_steps",
            execute_node if execute_node is not None else _execute_steps_noop,
        ),
        ("verify_results", verify_node if verify_node is not None else _verify_results_noop),
        ("recover_or_replan", recover_or_replan),
        ("finalize", finalize),
    ]
    # Registered in a loop — langgraph's _Node protocol does not type-check
    # node functions passed through a collection, hence the ignore.
    for name, node in nodes:
        if checkpoint_saver is not None:
            node = checkpointed(name, node, checkpoint_saver)  # type: ignore[assignment]
        builder.add_node(name, node)  # type: ignore[arg-type]

    builder.add_edge(START, "check_deadline")
    builder.add_conditional_edges(
        "check_deadline",
        _route_after_deadline,
        {"finalize": "finalize", "classify_intent": "classify_intent"},
    )
    builder.add_edge("classify_intent", "retrieve_context")
    builder.add_conditional_edges(
        "retrieve_context",
        _route_to_planner,
        {
            "build_plan": "build_plan",
            "build_plan_llm": "build_plan_llm",
            "validate_plan": "validate_plan",
        },
    )
    builder.add_edge("build_plan", "validate_plan")
    builder.add_edge("build_plan_llm", "validate_plan")
    builder.add_conditional_edges(
        "validate_plan",
        _route_after_validate,
        {"policy_check": "policy_check", "recover_or_replan": "recover_or_replan"},
    )
    builder.add_edge("policy_check", "request_approval")
    builder.add_conditional_edges(
        "request_approval",
        _route_after_approval,
        {"waiting": END, "proceed": "execute_ready_steps"},
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
            "build_plan_llm": "build_plan_llm",
            "finalize": "finalize",
        },
    )
    builder.add_edge("finalize", END)

    return builder.compile()
