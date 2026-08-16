"""Unit tests for the build_plan node — task 4.6.

The node is where the LLM turns the constrained inputs into a Plan DAG. It
receives intent-filtered tool candidates (DOMAIN_TOOL_MAP, 3-8, never the full
registry), L1 active skills (hard constraints) and L2 retrieved knowledge
(reference), and assembles the prompt in that exact injection order (docs/03
section 4). The LLM is injected as a plain callable so the node is testable
without a live provider; the app wires a real client at startup.

Two deterministic guards wrap the LLM output: parse_plan_response validates the
JSON against the strict Plan schema (extra="forbid"), and reject_l1_violations
drops steps that break a hard L1 rule — e.g. "任何状态 → CREATED" from the
order-state-machine skill (the canonical illegal transition).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from erp_copilot.agent.nodes.build_plan import (
    SYSTEM_PROMPT,
    build_plan_node,
    build_planner_prompt,
    parse_plan_response,
    reject_l1_violations,
)
from erp_copilot.agent.nodes.validate_plan import ToolSpec
from erp_copilot.agent.state import (
    AgentState,
    IntentClassification,
    Plan,
    PlanStep,
    RetrievedDocument,
)
from erp_copilot.retrieval.skill_matcher import match_skills
from erp_copilot.tools.candidate_filter import filter_candidates


def _step(**overrides: object) -> dict:
    base = {
        "step_id": "s1",
        "tool_name": "getProductById",
        "depends_on": [],
        "description": "查询商品",
        "arguments": {"id": 1},
        "argument_sources": {"id": "user_query"},
        "risk_level": "READ",
    }
    base.update(overrides)
    return base


def _plan_json(steps: list[dict], title: str = "test-plan") -> str:
    return json.dumps({"title": title, "steps": steps})


def _noop_llm(raw: str) -> str:
    return _plan_json([_step()])


def _agent_state(**overrides: object) -> AgentState:
    data: dict[str, object] = {
        "run_id": "run-1",
        "tenant_id": "t1",
        "query": "查询苹果",
        "intent": IntentClassification(domain="product", action="query"),
    }
    data.update(overrides)
    return AgentState(**data)


class TestBuildPlannerPrompt:
    def test_system_prompt_forbids_tool_calls(self) -> None:
        assert "绝不能调用" in SYSTEM_PROMPT
        assert "Plan" in SYSTEM_PROMPT

    def test_injection_order_system_l1_l2_tools_query(self) -> None:
        skills = [{"skill_id": "order-state-machine", "content": "订单状态机规则"}]
        knowledge = [RetrievedDocument(content="取消订单流程", source="orders.md")]
        prompt = build_planner_prompt(
            query="查询订单",
            active_skills=skills,
            retrieved_context=knowledge,
            candidate_tools=["getOrderByOrderId"],
        )
        order = [
            prompt.index(anchor)
            for anchor in (
                "系统",
                "订单状态机规则",
                "取消订单流程",
                "getOrderByOrderId",
                "查询订单",
            )
        ]
        assert order == sorted(order)

    def test_l1_skill_appears_before_l2_knowledge(self) -> None:
        skills = [{"skill_id": "s1", "content": "L1硬约束内容"}]
        knowledge = [RetrievedDocument(content="L2参考内容", source="docs.md")]
        prompt = build_planner_prompt(
            query="q",
            active_skills=skills,
            retrieved_context=knowledge,
            candidate_tools=["t1"],
        )
        assert prompt.index("L1硬约束内容") < prompt.index("L2参考内容")

    def test_all_candidate_tools_listed(self) -> None:
        prompt = build_planner_prompt(
            query="q",
            active_skills=[],
            retrieved_context=[],
            candidate_tools=["getProductById", "getProductByName"],
        )
        assert "getProductById" in prompt and "getProductByName" in prompt

    def test_query_appears_last(self) -> None:
        prompt = build_planner_prompt(
            query="查询苹果的库存",
            active_skills=[],
            retrieved_context=[],
            candidate_tools=["getProductById"],
        )
        assert prompt.rstrip().endswith("查询苹果的库存")

    def test_injects_required_params_from_schemas(self) -> None:
        # The real LLM (e.g. DeepSeek) guessed "product_name" for getProductByName
        # because the prompt only listed bare tool names, failing validate_plan's
        # MISSING_REQUIRED_ARG gate. With schemas injected the prompt must state
        # the exact required parameter names.
        schemas = {
            "getProductByName": ToolSpec(name="getProductByName", required_params=["name"]),
            "createOrder": ToolSpec(
                name="createOrder",
                required_params=["product_id", "supplier_id", "quantity", "region"],
            ),
        }
        prompt = build_planner_prompt(
            query="下单",
            active_skills=[],
            retrieved_context=[],
            candidate_tools=["getProductByName", "createOrder"],
            tool_schemas=schemas,
        )
        assert "必填参数: name" in prompt
        assert "必填参数: product_id, supplier_id, quantity, region" in prompt

    def test_tool_names_only_when_no_schemas(self) -> None:
        prompt = build_planner_prompt(
            query="q",
            active_skills=[],
            retrieved_context=[],
            candidate_tools=["getProductByName"],
        )
        # Without schemas no tool carries a "(必填参数: ...)" suffix; the
        # SYSTEM_PROMPT's generic mention of "必填参数名" is a separate string.
        assert "必填参数: " not in prompt

    def test_system_prompt_requires_fallback_and_bans_pending_source(self) -> None:
        # WRITE/DANGEROUS steps without a compensation note fail validate_plan's
        # WRITE_NEEDS_FALLBACK gate, so the planner prompt must demand one. And
        # "pending" is not a resolvable argument source (execute_steps fails it
        # with ARGUMENT_RESOLUTION_FAILED) — it must not be offered as legal.
        assert "fallback" in SYSTEM_PROMPT
        assert "补偿" in SYSTEM_PROMPT
        assert "pending" not in SYSTEM_PROMPT

    def test_system_prompt_uses_step_refs_not_user_query_for_sources(self) -> None:
        # "user_query" as an argument source makes execute_steps replace the
        # argument with the ENTIRE raw query at run time (resolve_arguments),
        # which mangled every entity param — name became the whole query
        # sentence, so getProductByName hit the cloud as PRODUCT_NOT_FOUND. The
        # contract: concrete values go straight into arguments; argument_sources
        # only ever holds "step:{id}" refs to other steps' outputs.
        assert "user_query" not in SYSTEM_PROMPT
        assert "step:{step_id}" in SYSTEM_PROMPT
        assert "具体值" in SYSTEM_PROMPT


class TestParsePlanResponse:
    def test_parses_object_with_steps(self) -> None:
        plan = parse_plan_response(_plan_json([_step()]))
        assert len(plan.steps) == 1
        assert plan.steps[0].tool_name == "getProductById"

    def test_parses_bare_list(self) -> None:
        plan = parse_plan_response(json.dumps([_step()]))
        assert len(plan.steps) == 1

    def test_strips_json_code_fence(self) -> None:
        raw = "```json\n" + _plan_json([_step()]) + "\n```"
        plan = parse_plan_response(raw)
        assert plan.steps[0].step_id == "s1"

    def test_unknown_step_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse_plan_response(_plan_json([_step(bogus_field=1)]))

    def test_missing_step_id_rejected(self) -> None:
        bad = {"steps": [{"tool_name": "getProductById"}], "title": "t"}
        with pytest.raises(ValidationError):
            parse_plan_response(json.dumps(bad))

    def test_coerces_numeric_step_ids_and_dependencies(self) -> None:
        # DeepSeek emitted step_id/depends_on as ints (1, 2, 3), which the strict
        # Plan schema rejects. The parser normalizes ints to strings so a
        # numerically consistent DAG (step "1" refs step:1) still validates —
        # type-sloppy LLM output is normalized the same way risk_level aliases are.
        plan = parse_plan_response(
            _plan_json(
                [
                    _step(step_id=1, depends_on=[2]),
                    _step(step_id=2, depends_on=[]),
                ]
            )
        )
        assert [s.step_id for s in plan.steps] == ["1", "2"]
        assert plan.steps[0].depends_on == ["2"]


class TestRejectL1Violations:
    # The order-state-machine skill's "非法转换" block forbids 任何状态 → CREATED.
    # match_skills("order", "update_status") resolves to that skill via the YAML map.
    def test_rejects_any_state_to_created_transition(self) -> None:
        skills = match_skills("order", "update_status")
        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1", tool_name="updateOrderStatus", arguments={"status": "CREATED"}
                )
            ]
        )
        kept, errors = reject_l1_violations(plan, skills)
        assert kept.steps == []
        assert errors[0].code == "L1_VIOLATION"
        assert errors[0].step_id == "s1"

    def test_keeps_legal_transition(self) -> None:
        skills = match_skills("order", "update_status")
        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1", tool_name="updateOrderStatus", arguments={"status": "CONFIRMED"}
                )
            ]
        )
        kept, errors = reject_l1_violations(plan, skills)
        assert len(kept.steps) == 1
        assert errors == []

    def test_drops_only_violating_step_keeps_others_and_title(self) -> None:
        skills = match_skills("order", "update_status")
        plan = Plan(
            title="发货计划",
            steps=[
                PlanStep(
                    step_id="s1", tool_name="updateOrderStatus", arguments={"status": "CREATED"}
                ),
                PlanStep(
                    step_id="s2", tool_name="getOrderByOrderId", arguments={"order_id": "abc"}
                ),
            ],
        )
        kept, errors = reject_l1_violations(plan, skills)
        assert [s.step_id for s in kept.steps] == ["s2"]
        assert kept.title == "发货计划"

    def test_no_skills_no_rejection(self) -> None:
        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1", tool_name="updateOrderStatus", arguments={"status": "CREATED"}
                )
            ]
        )
        kept, errors = reject_l1_violations(plan, [])
        assert len(kept.steps) == 1
        assert errors == []

    def test_source_specific_rules_need_runtime_state_not_rejected_here(self) -> None:
        # "DELIVERED → 任何状态" is illegal but the current state is unknown at
        # plan time — the layered defence leaves it to verify_results (layer 4).
        skills = match_skills("order", "update_status")
        plan = Plan(
            steps=[PlanStep(step_id="s1", tool_name="cancelOrder", arguments={"order_id": "abc"})]
        )
        kept, errors = reject_l1_violations(plan, skills)
        assert len(kept.steps) == 1
        assert errors == []

    def test_case_insensitive_state_match(self) -> None:
        skills = match_skills("order", "update_status")
        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1", tool_name="updateOrderStatus", arguments={"status": "created"}
                )
            ]
        )
        kept, _ = reject_l1_violations(plan, skills)
        assert kept.steps == []


class TestBuildPlanNode:
    def test_node_sets_plan_candidates_and_skills(self) -> None:
        node = build_plan_node(llm_complete=_noop_llm)
        state = _agent_state()
        updates = node(state)
        assert updates["plan"].steps[0].tool_name == "getProductById"
        assert updates["candidate_tools"] == filter_candidates("product", "query")
        assert updates["active_skills"] == match_skills("product", "query")

    def test_node_prompt_uses_injection_order_and_candidates(self) -> None:
        captured: list[str] = []

        def llm(prompt: str) -> str:
            captured.append(prompt)
            return _plan_json([_step()])

        node = build_plan_node(llm_complete=llm)
        state = _agent_state(
            query="查上海供应商",
            intent=IntentClassification(domain="supplier", action="query"),
            retrieved_context=[RetrievedDocument(content="配送区域规则", source="suppliers.md")],
        )
        node(state)
        prompt = captured[0]
        assert "querySuppliersByDeliveryRegion" in prompt
        assert prompt.index("配送区域规则") > prompt.index("供应商")  # L2 after L1 skills
        assert prompt.rstrip().endswith("查上海供应商")

    def test_node_rejects_l1_violating_step_and_records_error(self) -> None:
        node = build_plan_node(
            llm_complete=lambda _: _plan_json(
                [_step(tool_name="updateOrderStatus", arguments={"status": "CREATED"})]
            )
        )
        state = _agent_state(intent=IntentClassification(domain="order", action="update_status"))
        updates = node(state)
        assert updates["plan"].steps == []
        assert updates["errors"][0].code == "L1_VIOLATION"

    def test_node_records_parse_error_on_bad_llm_output(self) -> None:
        node = build_plan_node(llm_complete=lambda _: "this is not json")
        updates = node(_agent_state())
        assert updates["plan"] is None
        assert updates["errors"][0].code == "PLAN_PARSE_ERROR"

    def test_node_handles_none_intent_with_empty_candidates(self) -> None:
        node = build_plan_node(llm_complete=_noop_llm)
        updates = node(_agent_state(intent=None))
        assert updates["candidate_tools"] == []
        assert updates["active_skills"] == []
        assert updates["plan"].steps[0].tool_name == "getProductById"

    def test_node_prompt_includes_required_params_when_schemas_given(self) -> None:
        captured: list[str] = []

        def llm(prompt: str) -> str:
            captured.append(prompt)
            return _plan_json([_step()])

        node = build_plan_node(
            llm_complete=llm,
            available_tools={"getProductByName", "getProductById"},
            tool_schemas={
                "getProductByName": ToolSpec(name="getProductByName", required_params=["name"])
            },
        )
        node(_agent_state(intent=IntentClassification(domain="product", action="query")))
        assert "必填参数: name" in captured[0]

    def test_node_stamps_idempotency_key_on_write_steps(self) -> None:
        # The deterministic planner stamps {run_id}:{step_id} onto WRITE steps so
        # execute_steps routes them through the IdempotencyStore (at-most-once).
        # The LLM planner must do the same — without a key the write path degrades
        # to at-least-once (the cloud createOrder has no idempotency field).
        node = build_plan_node(
            llm_complete=lambda _: _plan_json(
                [
                    _step(
                        step_id="s3",
                        tool_name="createOrder",
                        arguments={"quantity": 5, "region": "上海"},
                        risk_level="WRITE",
                        fallback="取消新建订单以补偿",
                    )
                ]
            ),
            available_tools={"createOrder"},
        )
        updates = node(
            _agent_state(
                run_id="run-abc", intent=IntentClassification(domain="order", action="create")
            )
        )
        step = updates["plan"].steps[0]
        assert step.idempotency_key == "run-abc:s3"
        assert step.arguments["idempotency_key"] == "run-abc:s3"

    def test_node_promotes_literal_step_refs_to_sources(self) -> None:
        # DeepSeek put the dependency reference as a literal argument value
        # ({"product_id": "step:1"}) with empty argument_sources. resolve_arguments
        # only resolves refs listed in argument_sources, so without promotion the
        # literal "step:1" would be sent to the cloud as productId — a 302. The
        # node promotes "step:{id}"-shaped values into argument_sources.
        node = build_plan_node(
            llm_complete=lambda _: _plan_json(
                [
                    _step(
                        step_id="4",
                        tool_name="createOrder",
                        arguments={
                            "product_id": "step:1",
                            "supplier_id": "step:2",
                            "quantity": 5,
                        },
                        risk_level="WRITE",
                        fallback="取消新建订单以补偿",
                        depends_on=["1", "2"],
                    )
                ]
            ),
            available_tools={"createOrder"},
        )
        updates = node(
            _agent_state(intent=IntentClassification(domain="order", action="create"))
        )
        step = updates["plan"].steps[0]
        assert step.argument_sources["product_id"] == "step:1"
        assert step.argument_sources["supplier_id"] == "step:2"
