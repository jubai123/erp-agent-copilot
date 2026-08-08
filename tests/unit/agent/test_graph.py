"""Unit tests for agent/graph.py — the LangGraph skeleton (task 4.2).

The skeleton wires all nine Phase-4 nodes (request_approval is deferred to
Phase 5 / task 5.2). Acceptance: the graph contains all 9 nodes, the edges
declare the legal state transitions, the graph compiles, and mermaid output
is produced. A smoke run proves the happy path reaches SUCCEEDED.
"""

from __future__ import annotations

from erp_copilot.agent.graph import NODE_NAMES, build_agent_graph
from erp_copilot.agent.nodes.build_plan import build_plan_node
from erp_copilot.agent.nodes.retrieve_context import build_retrieve_context_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.state import StateError

GRAPH = build_agent_graph()


def _node_names() -> set[str]:
    return set(GRAPH.get_graph().nodes.keys())


def _edge_pairs() -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in GRAPH.get_graph().edges}


class TestTopology:
    def test_has_all_nine_nodes(self) -> None:
        assert set(NODE_NAMES) <= _node_names()

    def test_happy_path_chain_present(self) -> None:
        pairs = _edge_pairs()
        for source, target in [
            ("classify_intent", "retrieve_context"),
            ("retrieve_context", "build_plan"),
            ("build_plan", "validate_plan"),
            ("execute_ready_steps", "verify_results"),
        ]:
            assert (source, target) in pairs

    def test_validate_can_route_to_policy_or_recovery(self) -> None:
        pairs = _edge_pairs()
        assert ("validate_plan", "policy_check") in pairs
        assert ("validate_plan", "recover_or_replan") in pairs

    def test_policy_can_route_to_execute_or_finalize(self) -> None:
        pairs = _edge_pairs()
        assert ("policy_check", "execute_ready_steps") in pairs
        assert ("policy_check", "finalize") in pairs

    def test_verify_can_route_to_finalize_or_recovery(self) -> None:
        pairs = _edge_pairs()
        assert ("verify_results", "finalize") in pairs
        assert ("verify_results", "recover_or_replan") in pairs

    def test_recovery_can_route_three_ways(self) -> None:
        pairs = _edge_pairs()
        for target in ("build_plan", "execute_ready_steps", "finalize"):
            assert ("recover_or_replan", target) in pairs

    def test_start_and_end_termination(self) -> None:
        pairs = _edge_pairs()
        assert ("__start__", "classify_intent") in pairs
        assert ("finalize", "__end__") in pairs


class TestVisualization:
    def test_mermaid_output_contains_nodes(self) -> None:
        mermaid = GRAPH.get_graph().draw_mermaid()
        assert isinstance(mermaid, str) and len(mermaid) > 0
        assert "classify_intent" in mermaid
        assert "verify_results" in mermaid


class TestSmoke:
    def test_happy_path_run_reaches_succeeded(self) -> None:
        result = GRAPH.invoke({"run_id": "r1", "tenant_id": "t1", "query": "查苹果库存"})
        assert result["status"] == "succeeded"

    def test_injected_retrieve_node_populates_state(self) -> None:
        fake = build_retrieve_context_node(
            embed=lambda q: [1.0],
            vector_search=lambda emb, k, tid: [
                {
                    "chunk_id": "c1",
                    "content": "库存规则",
                    "section_path": ["库存"],
                    "char_count": 4,
                    "source": "orders.md",
                }
            ],
            keyword_search=lambda q, k, tid: [],
        )
        graph = build_agent_graph(retrieve_node=fake)
        result = graph.invoke({"run_id": "r3", "tenant_id": "t1", "query": "查苹果库存"})
        assert result["retrieved_context"][0].source == "orders.md"
        assert result["status"] == "succeeded"

    def test_injected_build_plan_node_populates_state(self) -> None:
        fake = build_plan_node(
            llm_complete=lambda _: (
                '{"title": "t", "steps": '
                '[{"step_id": "s1", "tool_name": "getProductById", "arguments": {"id": 1}}]}'
            )
        )
        graph = build_agent_graph(plan_node=fake)
        result = graph.invoke({"run_id": "r4", "tenant_id": "t1", "query": "查询苹果"})
        assert result["plan"].steps[0].tool_name == "getProductById"
        assert result["candidate_tools"]
        assert result["status"] == "succeeded"

    def test_injected_validate_node_rejects_invalid_plan(self) -> None:
        validate = build_validate_plan_node(
            tool_schemas={"getProductById": ToolSpec(name="getProductById", required_params=["id"])}
        )
        graph = build_agent_graph(validate_node=validate)
        result = graph.invoke(
            {
                "run_id": "r5",
                "tenant_id": "t1",
                "query": "查询苹果",
                "plan": {"steps": [{"step_id": "s1", "tool_name": "ghostTool"}]},
            }
        )
        assert result["plan_validation"].is_valid is False
        assert result["errors"][0].code == "UNKNOWN_TOOL"

    def test_error_path_routes_to_recovery(self) -> None:
        result = GRAPH.invoke(
            {
                "run_id": "r2",
                "tenant_id": "t1",
                "query": "q",
                "errors": [StateError(code="TRANSIENT_TOOL_ERROR", message="timeout")],
            }
        )
        # classify_intent set PLANNING before the recovery detour; the stub
        # recovery/finalize nodes do not change status yet.
        assert result["status"] == "planning"
