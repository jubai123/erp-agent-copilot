"""Unit tests for agent/graph.py — the LangGraph skeleton (task 4.2).

The skeleton wires all nine Phase-4 nodes (request_approval is deferred to
Phase 5 / task 5.2). Acceptance: the graph contains all 9 nodes, the edges
declare the legal state transitions, the graph compiles, and mermaid output
is produced. A smoke run proves the happy path reaches SUCCEEDED.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from erp_copilot.agent.graph import NODE_NAMES, build_agent_graph
from erp_copilot.agent.nodes.build_plan import build_plan_node
from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node
from erp_copilot.agent.nodes.policy_check import build_policy_check_node
from erp_copilot.agent.nodes.recover_or_replan import MAX_REPLANS, MAX_RETRIES
from erp_copilot.agent.nodes.retrieve_context import build_retrieve_context_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.nodes.verify_results import build_verify_results_node
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    ApprovalStatus,
    Plan,
    PlanStep,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.tools.tool_result import ToolResult

GRAPH = build_agent_graph()


class _RecordingSaver:
    """Duck-typed stand-in for CheckpointSaver — the graph only calls save()."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, AgentState]] = []

    def save(self, node_name: str, state: AgentState) -> None:
        self.saved.append((node_name, state))


def _node_names() -> set[str]:
    return set(GRAPH.get_graph().nodes.keys())


def _edge_pairs() -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in GRAPH.get_graph().edges}


class TestTopology:
    def test_has_all_named_nodes(self) -> None:
        assert set(NODE_NAMES) <= _node_names()

    def test_happy_path_chain_present(self) -> None:
        pairs = _edge_pairs()
        for source, target in [
            ("classify_intent", "retrieve_context"),
            ("retrieve_context", "build_plan"),
            ("retrieve_context", "build_plan_llm"),
            ("build_plan", "validate_plan"),
            ("build_plan_llm", "validate_plan"),
            ("policy_check", "request_approval"),
            ("execute_ready_steps", "verify_results"),
        ]:
            assert (source, target) in pairs

    def test_validate_can_route_to_policy_or_recovery(self) -> None:
        pairs = _edge_pairs()
        assert ("validate_plan", "policy_check") in pairs
        assert ("validate_plan", "recover_or_replan") in pairs

    def test_policy_routes_through_request_approval(self) -> None:
        pairs = _edge_pairs()
        assert ("policy_check", "request_approval") in pairs

    def test_request_approval_can_route_to_execute_or_end(self) -> None:
        pairs = _edge_pairs()
        assert ("request_approval", "execute_ready_steps") in pairs
        assert ("request_approval", "__end__") in pairs

    def test_verify_can_route_to_finalize_or_recovery(self) -> None:
        pairs = _edge_pairs()
        assert ("verify_results", "finalize") in pairs
        assert ("verify_results", "recover_or_replan") in pairs

    def test_recovery_can_route_three_ways(self) -> None:
        pairs = _edge_pairs()
        for target in ("build_plan", "build_plan_llm", "execute_ready_steps", "finalize"):
            assert ("recover_or_replan", target) in pairs

    def test_start_and_end_termination(self) -> None:
        pairs = _edge_pairs()
        assert ("__start__", "check_deadline") in pairs
        assert ("check_deadline", "classify_intent") in pairs
        assert ("finalize", "__end__") in pairs

    def test_deadline_can_route_to_finalize(self) -> None:
        pairs = _edge_pairs()
        assert ("check_deadline", "finalize") in pairs


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
        # The validator flags the unknown tool; the recovery node replans once,
        # and with a no-op planner the rerun still has no valid plan, so the
        # run gives up FAILED instead of executing the bad plan.
        assert result["plan_validation"].is_valid is False
        assert result["status"] == AgentStatus.FAILED
        assert result["replan_count"] == 1

    def test_injected_policy_node_populates_state(self) -> None:
        policy = build_policy_check_node(get_scopes=lambda _t, _u: {"product:read"})
        graph = build_agent_graph(policy_node=policy)
        result = graph.invoke(
            {
                "run_id": "r6",
                "tenant_id": "t1",
                "user_id": "u1",
                "query": "查询苹果",
                "plan": {
                    "steps": [
                        {
                            "step_id": "s1",
                            "tool_name": "getProductById",
                            "required_scope": "product:read",
                        }
                    ]
                },
            }
        )
        assert result["policy_decisions"]["s1"] == "allow"
        assert result["status"] == "succeeded"

    def test_require_approval_run_pauses_at_waiting_approval(self) -> None:
        policy = build_policy_check_node(get_scopes=lambda _t, _u: {"order:write"})
        graph = build_agent_graph(policy_node=policy)
        result = graph.invoke(
            {
                "run_id": "r11",
                "tenant_id": "t1",
                "user_id": "u1",
                "query": "帮我在上海下一单 1 KG 苹果",
                "plan": {
                    "steps": [
                        {
                            "step_id": "s1",
                            "tool_name": "createOrder",
                            "risk_level": "WRITE",
                            "required_scope": "order:write",
                        }
                    ]
                },
            }
        )
        assert result["status"] == AgentStatus.WAITING_APPROVAL
        assert result["approvals"][0].step_id == "s1"
        assert result["approvals"][0].status == ApprovalStatus.PENDING

    def test_approved_run_resumes_past_request_approval(self) -> None:
        # Simulates the checkpoint-resume path after a human approves: the
        # APPROVED record rides in the state, policy re-decides REQUIRE_APPROVAL
        # on re-invoke, but request_approval sees the decided record and lets
        # the run proceed to execution.
        policy = build_policy_check_node(get_scopes=lambda _t, _u: {"order:write"})
        graph = build_agent_graph(policy_node=policy)
        result = graph.invoke(
            {
                "run_id": "r12",
                "tenant_id": "t1",
                "user_id": "u1",
                "query": "帮我在上海下一单 1 KG 苹果",
                "plan": {
                    "steps": [
                        {
                            "step_id": "s1",
                            "tool_name": "createOrder",
                            "risk_level": "WRITE",
                            "required_scope": "order:write",
                        }
                    ]
                },
                "approvals": [
                    ApprovalRequest(
                        step_id="s1",
                        tool_name="createOrder",
                        risk_level=ToolRiskLevel.WRITE,
                        status=ApprovalStatus.APPROVED,
                    )
                ],
            }
        )
        assert result["status"] == AgentStatus.SUCCEEDED
        assert result["approvals"][0].status == ApprovalStatus.APPROVED

    def test_injected_execute_node_populates_state(self) -> None:
        validate = build_validate_plan_node(
            tool_schemas={"getProductById": ToolSpec(name="getProductById", required_params=["id"])}
        )
        policy = build_policy_check_node(get_scopes=lambda _t, _u: set())

        async def fake_executor(_tool_name: str, arguments: dict[str, object]) -> ToolResult:
            return ToolResult.success(
                tool_version_id="v1", data={"name": "苹果", "id": arguments["id"]}
            )

        execute = build_execute_steps_node(executor=fake_executor)
        graph = build_agent_graph(validate_node=validate, policy_node=policy, execute_node=execute)
        result = asyncio.run(
            graph.ainvoke(
                {
                    "run_id": "r7",
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "查询苹果",
                    "plan": {
                        "steps": [
                            {
                                "step_id": "s1",
                                "tool_name": "getProductById",
                                "arguments": {"id": 1},
                            }
                        ]
                    },
                }
            )
        )
        assert result["step_results"]["s1"].status == "completed"
        assert result["step_results"]["s1"].data["name"] == "苹果"
        assert result["status"] == "succeeded"

    def test_injected_verify_node_populates_state(self) -> None:
        verify = build_verify_results_node()
        graph = build_agent_graph(verify_node=verify)
        result = graph.invoke(
            {
                "run_id": "r8",
                "tenant_id": "t1",
                "user_id": "u1",
                "query": "查苹果",
                "plan": {
                    "steps": [
                        {
                            "step_id": "s1",
                            "tool_name": "getProductById",
                            "success_condition": "response.name == '苹果'",
                        }
                    ]
                },
                "step_results": {
                    "s1": StepResult(
                        step_id="s1", status=StepStatus.COMPLETED, data={"name": "苹果"}
                    )
                },
            }
        )
        assert result["status"] == "succeeded"

    def test_verify_semantic_failure_routes_to_recovery(self) -> None:
        verify = build_verify_results_node()
        graph = build_agent_graph(verify_node=verify)
        result = graph.invoke(
            {
                "run_id": "r9",
                "tenant_id": "t1",
                "user_id": "u1",
                "query": "查苹果",
                "plan": {
                    "steps": [
                        {
                            "step_id": "s1",
                            "tool_name": "getProductById",
                            "success_condition": "response.name == '苹果'",
                        }
                    ]
                },
                "step_results": {
                    "s1": StepResult(
                        step_id="s1", status=StepStatus.COMPLETED, data={"name": "橙子"}
                    )
                },
            }
        )
        # The semantic mismatch classifies the run REPLANNING; the recovery node
        # replans once, and with a no-op planner the rerun still has no plan, so
        # the run gives up FAILED instead of executing the wrong-data step again.
        assert result["status"] == AgentStatus.FAILED
        assert result["replan_count"] == 1

    def test_checkpoint_saver_records_every_node(self) -> None:
        saver = _RecordingSaver()
        graph = build_agent_graph(checkpoint_saver=saver)  # type: ignore[arg-type]
        result = graph.invoke({"run_id": "r10", "tenant_id": "t1", "query": "查苹果库存"})
        assert result["status"] == "succeeded"
        names = [name for name, _ in saver.saved]
        assert names == [
            "check_deadline",
            "classify_intent",
            "retrieve_context",
            "build_plan",
            "validate_plan",
            "policy_check",
            "request_approval",
            "execute_ready_steps",
            "verify_results",
            "finalize",
        ]
        assert saver.saved[-1][1].status == "succeeded"

    def test_error_path_replans_once_then_succeeds(self) -> None:
        result = GRAPH.invoke(
            {
                "run_id": "r2",
                "tenant_id": "t1",
                "query": "查苹果库存",
                "errors": [StateError(code="TRANSIENT_TOOL_ERROR", message="timeout")],
            }
        )
        # The real recovery node consumes the detour-triggering error as one
        # replan (status PLANNING -> recover -> build_plan), clears it, then
        # the no-op graph proceeds clean to SUCCEEDED.
        assert result["status"] == "succeeded"
        assert result["replan_count"] == 1
        assert result["errors"] == []


class TestFunnelRouting:
    """The three-layer funnel routes the query to the right plan node."""

    def test_tier2_without_llm_node_fails_honestly(self) -> None:
        # "随便聊聊" falls to product/query with no product entity -> EMPTY_PLAN
        # -> route_query_layer tier2. With no injected LLM plan node the graph
        # must fail honestly (ROUTED_TIER23_NO_LLM), never over-grab the query.
        saver = _RecordingSaver()
        graph = build_agent_graph(checkpoint_saver=saver)  # type: ignore[arg-type]
        result = graph.invoke({"run_id": "r-t2", "tenant_id": "t1", "query": "随便聊聊"})
        assert result["status"] == AgentStatus.FAILED
        assert result["replan_count"] == MAX_REPLANS
        names = [name for name, _ in saver.saved]
        assert "build_plan_llm" in names
        assert "build_plan" not in names

    def test_tier2_with_injected_llm_node_uses_it(self) -> None:
        calls: list[str] = []
        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="getProductById",
                    description="按名称查询商品",
                    arguments={"name": "苹果"},
                )
            ]
        )

        def fake_llm(state: AgentState) -> dict[str, object]:
            calls.append(state.query)
            return {"plan": plan, "candidate_tools": ["getProductById"]}

        saver = _RecordingSaver()
        graph = build_agent_graph(llm_plan_node=fake_llm, checkpoint_saver=saver)  # type: ignore[arg-type]
        result = graph.invoke({"run_id": "r-t2llm", "tenant_id": "t1", "query": "随便聊聊"})
        assert calls == ["随便聊聊"]
        assert result["plan"].steps[0].tool_name == "getProductById"
        assert result["status"] == AgentStatus.SUCCEEDED
        names = [name for name, _ in saver.saved]
        assert "build_plan_llm" in names
        assert "build_plan" not in names

    def test_tier1_routes_to_deterministic_plan_node(self) -> None:
        plan_calls: list[str] = []
        llm_calls: list[str] = []

        def recording_plan(state: AgentState) -> dict[str, object]:
            plan_calls.append(state.query)
            return {}

        def recording_llm(state: AgentState) -> dict[str, object]:
            llm_calls.append(state.query)
            return {"plan": None, "errors": []}

        graph = build_agent_graph(plan_node=recording_plan, llm_plan_node=recording_llm)
        result = graph.invoke({"run_id": "r-t1", "tenant_id": "t1", "query": "查苹果库存"})
        assert plan_calls == ["查苹果库存"]
        assert llm_calls == []
        assert result["status"] == AgentStatus.SUCCEEDED

    def test_resumed_run_with_existing_plan_skips_planner(self) -> None:
        # A resumed run (checkpoint replay, approve/deny resume) already carries a
        # validated plan approved by a human on a previous pass. Re-entering the
        # planner — especially the LLM planner, whose step_ids are not positional —
        # would orphan the approval records and could swap in a plan nobody
        # approved, so the graph must route the state straight to re-validation.
        plan_calls: list[str] = []
        llm_calls: list[str] = []

        def recording_plan(state: AgentState) -> dict[str, object]:
            plan_calls.append(state.query)
            return {}

        def recording_llm(state: AgentState) -> dict[str, object]:
            llm_calls.append(state.query)
            return {"plan": None, "errors": []}

        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="getProductById",
                    description="按名称查询商品",
                    arguments={"name": "苹果"},
                )
            ]
        )
        graph = build_agent_graph(plan_node=recording_plan, llm_plan_node=recording_llm)
        # "随便聊聊" routes to tier2, but the existing plan must win on resume.
        result = graph.invoke(
            {"run_id": "r-resume", "tenant_id": "t1", "query": "随便聊聊", "plan": plan}
        )
        assert plan_calls == []
        assert llm_calls == []
        assert result["plan"].steps[0].step_id == "s1"
        assert result["status"] == AgentStatus.SUCCEEDED


class TestDeadline:
    """check_deadline expires runs past their deadline, else is a no-op."""

    def test_past_deadline_expires_run(self) -> None:
        result = GRAPH.invoke(
            {
                "run_id": "r-dl-past",
                "tenant_id": "t1",
                "query": "查苹果库存",
                "deadline_at": datetime(2020, 1, 1, tzinfo=UTC),
            }
        )
        assert result["status"] == AgentStatus.EXPIRED
        assert result["errors"][0].code == "DEADLINE_EXCEEDED"

    def test_future_deadline_proceeds_to_success(self) -> None:
        result = GRAPH.invoke(
            {
                "run_id": "r-dl-future",
                "tenant_id": "t1",
                "query": "查苹果库存",
                "deadline_at": datetime(2100, 1, 1, tzinfo=UTC),
            }
        )
        assert result["status"] == AgentStatus.SUCCEEDED

    def test_no_deadline_is_a_no_op(self) -> None:
        result = GRAPH.invoke({"run_id": "r-dl-none", "tenant_id": "t1", "query": "查苹果库存"})
        assert result["status"] == AgentStatus.SUCCEEDED


class TestRecoveryLoop:
    """recover_or_replan drives real retry/replan loops in the compiled graph."""

    def _failing_graph(self, is_retryable: bool):
        validate = build_validate_plan_node(
            tool_schemas={"getProductById": ToolSpec(name="getProductById", required_params=["id"])}
        )
        policy = build_policy_check_node(get_scopes=lambda _t, _u: {"product:read"})

        async def executor(tool_name: str, arguments: dict[str, object]) -> ToolResult:
            return ToolResult.failure(
                tool_version_id=tool_name,
                error_code="TIMEOUT" if is_retryable else "PERMISSION_DENIED",
                error_message="boom",
                is_retryable=is_retryable,
            )

        return build_agent_graph(
            validate_node=validate,
            policy_node=policy,
            execute_node=build_execute_steps_node(executor=executor),
            verify_node=build_verify_results_node(),
        )

    def test_retryable_failure_exhausts_retry_budget_then_gives_up(self) -> None:
        graph = self._failing_graph(is_retryable=True)
        result = asyncio.run(
            graph.ainvoke(
                {
                    "run_id": "r-loop-retry",
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "查询苹果",
                    "plan": {
                        "steps": [
                            {
                                "step_id": "s1",
                                "tool_name": "getProductById",
                                "arguments": {"id": 1},
                            }
                        ]
                    },
                }
            )
        )
        assert result["status"] == AgentStatus.FAILED
        assert result["retry_count"] == MAX_RETRIES
        assert result["errors"][-1].code == "RETRY_BUDGET_EXHAUSTED"

    def test_permanent_failure_exhausts_replan_budget_then_gives_up(self) -> None:
        graph = self._failing_graph(is_retryable=False)
        result = asyncio.run(
            graph.ainvoke(
                {
                    "run_id": "r-loop-replan",
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "查询苹果",
                    "plan": {
                        "steps": [
                            {
                                "step_id": "s1",
                                "tool_name": "getProductById",
                                "arguments": {"id": 1},
                            }
                        ]
                    },
                }
            )
        )
        assert result["status"] == AgentStatus.FAILED
        assert result["replan_count"] == MAX_REPLANS
        assert result["errors"][-1].code == "REPLAN_BUDGET_EXHAUSTED"
