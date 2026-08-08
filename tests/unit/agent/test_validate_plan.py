"""Unit tests for the validate_plan node — task 4.7.

The node is defence layer 2: a deterministic gate on the LLM's Plan DAG. It
rejects cycles, unknown tools, tools outside the intent-filtered candidate set,
missing required arguments, argument_sources that reference steps not in
depends_on, and WRITE steps without a compensation/fallback note. It also
computes the topological order and the parallelisable step groups (READ steps
group together; WRITE steps run serially) for the executor.

Semantic misselection (getProductById with id="苹果") is deliberately NOT caught
here — the strict-schema PlanStep accepts it — and is left to verify_results
(layer 4). Tool schemas are injected as pure data so the validator is testable
without a database.
"""

from __future__ import annotations

from erp_copilot.agent.nodes.validate_plan import (
    ToolSpec,
    build_validate_plan_node,
    validate_plan,
)
from erp_copilot.agent.state import AgentState, Plan, PlanStep, PlanValidation, StateError
from erp_copilot.domain.enums import ToolRiskLevel

TOOLS: dict[str, ToolSpec] = {
    "getProductById": ToolSpec(name="getProductById", required_params=["id"]),
    "getOrderByOrderId": ToolSpec(name="getOrderByOrderId", required_params=["order_id"]),
    "updateOrderStatus": ToolSpec(name="updateOrderStatus", required_params=["order_id", "status"]),
}


def _step(
    step_id: str = "s1",
    tool_name: str = "getProductById",
    **overrides: object,
) -> PlanStep:
    data: dict[str, object] = {
        "step_id": step_id,
        "tool_name": tool_name,
        "depends_on": [],
        "arguments": {"id": 1},
        "argument_sources": {"id": "user_query"},
    }
    data.update(overrides)
    return PlanStep(**data)


def _codes(validation: PlanValidation) -> list[str]:
    return [e.code for e in validation.errors]


class TestStructuralChecks:
    def test_valid_read_plan_passes(self) -> None:
        result = validate_plan(
            Plan(
                steps=[
                    _step(),
                    _step(
                        step_id="s2", tool_name="getOrderByOrderId", arguments={"order_id": "abc"}
                    ),
                ]
            ),
            TOOLS,
        )
        assert result.is_valid is True
        assert result.errors == []

    def test_unknown_tool_rejected(self) -> None:
        result = validate_plan(Plan(steps=[_step(tool_name="ghostTool")]), TOOLS)
        assert result.is_valid is False
        assert "UNKNOWN_TOOL" in _codes(result)

    def test_tool_outside_candidates_rejected(self) -> None:
        result = validate_plan(
            Plan(steps=[_step(tool_name="getProductById")]),
            TOOLS,
            candidate_tools=["getOrderByOrderId"],
        )
        assert "OUT_OF_CANDIDATES" in _codes(result)

    def test_tool_in_candidates_passes(self) -> None:
        result = validate_plan(
            Plan(steps=[_step(tool_name="getProductById")]),
            TOOLS,
            candidate_tools=["getProductById"],
        )
        assert result.is_valid is True

    def test_empty_candidates_skips_candidate_check(self) -> None:
        result = validate_plan(Plan(steps=[_step(tool_name="getProductById")]), TOOLS)
        assert result.is_valid is True

    def test_missing_required_param_rejected(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    tool_name="updateOrderStatus",
                    arguments={"order_id": "abc"},
                    argument_sources={"order_id": "user_query"},
                )
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "MISSING_REQUIRED_ARG" in _codes(result)

    def test_param_sourced_via_argument_sources_counts_as_present(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    tool_name="updateOrderStatus",
                    arguments={"order_id": "abc"},
                    argument_sources={"order_id": "user_query", "status": "pending"},
                )
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "MISSING_REQUIRED_ARG" not in _codes(result)

    def test_argument_source_ref_not_in_depends_on_rejected(self) -> None:
        plan = Plan(
            steps=[
                _step(step_id="s1", tool_name="getProductById"),
                _step(
                    step_id="s2",
                    tool_name="updateOrderStatus",
                    depends_on=[],
                    arguments={"order_id": "abc", "status": "CONFIRMED"},
                    argument_sources={
                        "order_id": "step:s1",
                        "status": "user_query",
                    },
                ),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "BROKEN_ARGUMENT_SOURCE" in _codes(result)

    def test_argument_source_ref_in_depends_on_passes(self) -> None:
        plan = Plan(
            steps=[
                _step(step_id="s1", tool_name="getProductById"),
                _step(
                    step_id="s2",
                    tool_name="updateOrderStatus",
                    depends_on=["s1"],
                    arguments={"order_id": "abc", "status": "CONFIRMED"},
                    argument_sources={
                        "order_id": "step:s1",
                        "status": "user_query",
                    },
                ),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert result.is_valid is True

    def test_argument_source_ref_to_missing_step_rejected(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    step_id="s2",
                    tool_name="updateOrderStatus",
                    depends_on=["ghost"],
                    arguments={"order_id": "abc", "status": "CONFIRMED"},
                    argument_sources={"order_id": "step:ghost", "status": "user_query"},
                )
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "BROKEN_ARGUMENT_SOURCE" in _codes(result)
        assert "MISSING_DEPENDENCY" in _codes(result)

    def test_missing_dependency_rejected(self) -> None:
        plan = Plan(steps=[_step(step_id="s1", depends_on=["ghost"])])
        result = validate_plan(plan, TOOLS)
        assert "MISSING_DEPENDENCY" in _codes(result)

    def test_write_step_without_fallback_rejected(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.WRITE,
                    arguments={"order_id": "a", "status": "X"},
                )
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "WRITE_NEEDS_FALLBACK" in _codes(result)

    def test_write_step_with_fallback_passes(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.WRITE,
                    arguments={"order_id": "a", "status": "X"},
                    fallback="rollback",
                )
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "WRITE_NEEDS_FALLBACK" not in _codes(result)

    def test_dangerous_step_without_fallback_rejected(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.DANGEROUS,
                    arguments={"order_id": "a", "status": "X"},
                )
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert "WRITE_NEEDS_FALLBACK" in _codes(result)

    def test_read_step_without_fallback_passes(self) -> None:
        result = validate_plan(Plan(steps=[_step()]), TOOLS)
        assert "WRITE_NEEDS_FALLBACK" not in _codes(result)


class TestTopologicalChecks:
    def test_cycle_detected(self) -> None:
        plan = Plan(
            steps=[
                _step(step_id="a", depends_on=["b"]),
                _step(step_id="b", depends_on=["a"]),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert result.is_valid is False
        assert "PLAN_CYCLE" in _codes(result)
        assert result.topological_order == []

    def test_linear_chain_ordered(self) -> None:
        plan = Plan(
            steps=[
                _step(step_id="a"),
                _step(step_id="b", depends_on=["a"]),
                _step(step_id="c", depends_on=["b"]),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert result.topological_order == ["a", "b", "c"]
        assert result.is_valid is True

    def test_independent_reads_run_in_one_parallel_group(self) -> None:
        plan = Plan(steps=[_step(step_id="a"), _step(step_id="b")])
        result = validate_plan(plan, TOOLS)
        assert result.parallel_groups == [["a", "b"]]

    def test_writes_are_serial_singleton_groups(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    step_id="w1",
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.WRITE,
                    arguments={"order_id": "a", "status": "X"},
                    fallback="rollback",
                ),
                _step(
                    step_id="w2",
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.WRITE,
                    arguments={"order_id": "b", "status": "X"},
                    fallback="rollback",
                ),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert result.parallel_groups == [["w1"], ["w2"]]

    def test_read_and_write_are_split(self) -> None:
        plan = Plan(
            steps=[
                _step(step_id="r", tool_name="getOrderByOrderId", arguments={"order_id": "a"}),
                _step(
                    step_id="w",
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.WRITE,
                    arguments={"order_id": "a", "status": "X"},
                    fallback="rollback",
                ),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert result.parallel_groups == [["w"], ["r"]]

    def test_dependent_chain_forms_sequential_waves(self) -> None:
        plan = Plan(
            steps=[
                _step(step_id="a"),
                _step(step_id="b", depends_on=["a"]),
            ]
        )
        result = validate_plan(plan, TOOLS)
        assert result.parallel_groups == [["a"], ["b"]]


class TestBuildValidatePlanNode:
    def test_node_marks_valid_plan_and_adds_no_errors(self) -> None:
        node = build_validate_plan_node(tool_schemas=TOOLS)
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            plan=Plan(steps=[_step()]),
        )
        updates = node(state)
        assert updates["plan_validation"].is_valid is True
        assert "errors" not in updates

    def test_node_appends_errors_to_state(self) -> None:
        node = build_validate_plan_node(tool_schemas=TOOLS)
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            plan=Plan(steps=[_step(tool_name="ghostTool")]),
        )
        updates = node(state)
        assert updates["plan_validation"].is_valid is False
        assert [e.code for e in updates["errors"]] == ["UNKNOWN_TOOL"]

    def test_node_preserves_existing_errors(self) -> None:
        node = build_validate_plan_node(tool_schemas=TOOLS)
        existing = StateError(code="EXISTING", message="x")
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            plan=Plan(steps=[_step(tool_name="ghostTool")]),
            errors=[existing],
        )
        updates = node(state)
        assert [e.code for e in updates["errors"]] == ["EXISTING", "UNKNOWN_TOOL"]

    def test_node_handles_missing_plan(self) -> None:
        node = build_validate_plan_node(tool_schemas=TOOLS)
        state = AgentState(run_id="r1", tenant_id="t1", query="q", plan=None)
        updates = node(state)
        assert updates["plan_validation"].is_valid is False
        assert [e.code for e in updates["errors"]] == ["NO_PLAN"]
