"""Unit tests for the execute_ready_steps node — tasks 4.9-4.10.

Defence layer 3: actually run the plan steps. The node walks
plan_validation.parallel_groups wave by wave; the READ steps in a wave run
concurrently via asyncio.gather, WRITE steps (singleton waves from
validate_plan) run serially. Each step's arguments are resolved at runtime
(user_query -> the run's query text; step:{id} -> the same-named output param
of that step's result) before the wave is launched. Only steps whose policy
decision is ALLOW run; a step whose dependencies did not reach COMPLETED is
skipped; a failed step records FAILED and appends a StateError so the graph
routes to recovery.

The executor is an async (tool_name, arguments) -> Awaitable[ToolResult]
callable — the real MCP Gateway is async and is injected directly. When
parallel_groups is empty (defensive; a valid plan always carries it) the node
falls back to serial execution over topological_order.
"""

from __future__ import annotations

import asyncio
from typing import Any

from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node, resolve_arguments
from erp_copilot.agent.state import (
    AgentState,
    Plan,
    PlanStep,
    PlanValidation,
    PolicyDecision,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.tools.tool_result import ToolResult


def _step(
    step_id: str = "s1",
    tool_name: str = "getProductById",
    **overrides: object,
) -> PlanStep:
    data: dict[str, object] = {
        "step_id": step_id,
        "tool_name": tool_name,
        "risk_level": ToolRiskLevel.READ,
        "depends_on": [],
        "arguments": {"id": 1},
        "argument_sources": {},
    }
    data.update(overrides)
    return PlanStep(**data)


def _completed(step_id: str, data: dict[str, object]) -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.COMPLETED, data=data)


def _node_state(
    *,
    plan: Plan,
    query: str = "q",
    validation: PlanValidation | None = None,
    decisions: dict[str, PolicyDecision] | None = None,
    existing: dict[str, StepResult] | None = None,
    errors: list[StateError] | None = None,
) -> AgentState:
    steps = plan.steps
    return AgentState(
        run_id="r1",
        tenant_id="t1",
        user_id="u1",
        query=query,
        plan=plan,
        plan_validation=validation
        or PlanValidation(is_valid=True, topological_order=[s.step_id for s in steps]),
        policy_decisions=decisions or {s.step_id: PolicyDecision.ALLOW for s in steps},
        step_results=existing or {},
        errors=errors or [],
    )


def _invoke(node: Any, state: AgentState) -> dict[str, Any]:
    return asyncio.run(node(state))


class TestResolveArguments:
    def test_user_query_source_injects_query(self) -> None:
        args, error = resolve_arguments(
            _step(argument_sources={"id": "user_query"}),
            query="苹果",
            step_results={},
        )
        assert error is None
        assert args == {"id": "苹果"}

    def test_step_source_pulls_same_named_output(self) -> None:
        step = _step(
            step_id="s2",
            tool_name="getOrderByOrderId",
            arguments={"order_id": "literal"},
            argument_sources={"order_id": "step:s1"},
        )
        results = {"s1": _completed("s1", {"order_id": "abc123", "name": "苹果订单"})}
        args, error = resolve_arguments(step, "q", results)
        assert error is None
        assert args == {"order_id": "abc123"}

    def test_literal_arguments_preserved_when_no_sources(self) -> None:
        args, error = resolve_arguments(_step(arguments={"id": 7}), "q", {})
        assert error is None
        assert args == {"id": 7}

    def test_source_overrides_literal_value(self) -> None:
        args, error = resolve_arguments(
            _step(arguments={"id": 1}, argument_sources={"id": "user_query"}),
            "苹果",
            {},
        )
        assert error is None
        assert args == {"id": "苹果"}

    def test_missing_dependency_result_is_failure(self) -> None:
        args, error = resolve_arguments(
            _step(step_id="s2", argument_sources={"id": "step:ghost"}),
            "q",
            {},
        )
        assert error is not None
        assert error.code == "ARGUMENT_RESOLUTION_FAILED"
        assert error.step_id == "s2"

    def test_missing_output_param_is_failure(self) -> None:
        step = _step(step_id="s2", argument_sources={"id": "step:s1"})
        results = {"s1": _completed("s1", {"name": "苹果"})}
        args, error = resolve_arguments(step, "q", results)
        assert error is not None
        assert error.code == "ARGUMENT_RESOLUTION_FAILED"

    def test_unknown_source_is_failure(self) -> None:
        args, error = resolve_arguments(
            _step(argument_sources={"id": "retrieved"}),
            "q",
            {},
        )
        assert error is not None
        assert error.code == "ARGUMENT_RESOLUTION_FAILED"


class TestBuildExecuteStepsNode:
    def test_executes_steps_in_wave_order(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1", tool_name="getProductById"),
                _step(
                    "s2",
                    tool_name="getOrderByOrderId",
                    arguments={"order_id": "abc"},
                    depends_on=["s1"],
                ),
            ]
        )
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1"], ["s2"]]),
        )
        _invoke(node, state)
        assert calls == ["getProductById", "getOrderByOrderId"]

    def test_empty_parallel_groups_falls_back_to_topological_order(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1", tool_name="getProductById"),
                _step(
                    "s2",
                    tool_name="getOrderByOrderId",
                    arguments={"order_id": "abc"},
                    depends_on=["s1"],
                ),
            ]
        )
        state = _node_state(plan=plan)
        _invoke(node, state)
        assert calls == ["getProductById", "getOrderByOrderId"]

    def test_independent_reads_run_concurrently(self) -> None:
        active = 0
        max_active = 0

        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1", "s2"]]),
        )
        _invoke(node, state)
        assert max_active == 2

    def test_dependent_chain_runs_in_sequential_waves(self) -> None:
        active = 0
        max_active = 0
        order: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            order.append(tool_name)
            await asyncio.sleep(0.02)
            active -= 1
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1", tool_name="getProductById"),
                _step(
                    "s2",
                    tool_name="getOrderByOrderId",
                    arguments={"order_id": "abc"},
                    depends_on=["s1"],
                ),
            ]
        )
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1"], ["s2"]]),
        )
        _invoke(node, state)
        assert max_active == 1
        assert order == ["getProductById", "getOrderByOrderId"]

    def test_write_step_does_not_run_in_parallel(self) -> None:
        active = 0
        max_active = 0

        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("r", tool_name="getOrderByOrderId", arguments={"order_id": "a"}),
                _step(
                    "w",
                    tool_name="updateOrderStatus",
                    risk_level=ToolRiskLevel.WRITE,
                    arguments={"order_id": "a", "status": "SHIPPED"},
                    fallback="rollback",
                ),
            ]
        )
        state = _node_state(
            plan=plan,
            decisions={"r": PolicyDecision.ALLOW, "w": PolicyDecision.ALLOW},
            validation=PlanValidation(is_valid=True, parallel_groups=[["w"], ["r"]]),
        )
        _invoke(node, state)
        assert max_active == 1

    def test_parallel_results_merge_independently_of_completion_order(self) -> None:
        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            if tool_name == "getProductById":
                await asyncio.sleep(0.05)
                return ToolResult.success(tool_version_id="v1", data={"kind": "product"})
            return ToolResult.success(tool_version_id="v1", data={"kind": "order"})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1", tool_name="getProductById"),
                _step("s2", tool_name="getOrderByOrderId", arguments={"order_id": "a"}),
            ]
        )
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1", "s2"]]),
        )
        updates = _invoke(node, state)
        assert updates["step_results"]["s1"].data == {"kind": "product"}
        assert updates["step_results"]["s2"].data == {"kind": "order"}

    def test_parallel_failure_does_not_block_siblings(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            if tool_name == "getProductById":
                return ToolResult.failure(
                    tool_version_id="v1", error_code="TIMEOUT", error_message="timeout"
                )
            return ToolResult.success(tool_version_id="v1", data={"order_id": "a"})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1", tool_name="getProductById"),
                _step("s2", tool_name="getOrderByOrderId", arguments={"order_id": "a"}),
            ]
        )
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1", "s2"]]),
        )
        updates = _invoke(node, state)
        assert set(calls) == {"getProductById", "getOrderByOrderId"}
        assert updates["step_results"]["s1"].status == StepStatus.FAILED
        assert updates["step_results"]["s2"].status == StepStatus.COMPLETED
        assert [e.code for e in updates["errors"]] == ["TIMEOUT"]

    def test_later_wave_resolves_step_source_from_prior_wave(self) -> None:
        seen: list[dict[str, Any]] = []

        async def executor(_tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            seen.append(arguments)
            return ToolResult.success(tool_version_id="v1", data=dict(arguments))

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1", arguments={"id": 7}),
                _step("s2", arguments={}, argument_sources={"id": "step:s1"}, depends_on=["s1"]),
            ]
        )
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1"], ["s2"]]),
        )
        _invoke(node, state)
        assert seen == [{"id": 7}, {"id": 7}]

    def test_user_query_source_resolved_before_executor(self) -> None:
        seen: list[dict[str, Any]] = []

        async def executor(_tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            seen.append(arguments)
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(steps=[_step(argument_sources={"id": "user_query"})])
        state = _node_state(plan=plan, query="苹果库存")
        _invoke(node, state)
        assert seen == [{"id": "苹果库存"}]

    def test_successful_step_records_completed(self) -> None:
        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.success(tool_version_id="v1", data={"price": 10})

        node = build_execute_steps_node(executor=executor)
        updates = _invoke(node, _node_state(plan=Plan(steps=[_step()])))
        assert updates["step_results"]["s1"].status == StepStatus.COMPLETED
        assert updates["step_results"]["s1"].data == {"price": 10}

    def test_failed_step_records_failed_and_appends_error(self) -> None:
        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.failure(
                tool_version_id="v1",
                error_code="TIMEOUT",
                error_message="gateway timeout",
                is_retryable=True,
            )

        node = build_execute_steps_node(executor=executor)
        updates = _invoke(node, _node_state(plan=Plan(steps=[_step()])))
        result = updates["step_results"]["s1"]
        assert result.status == StepStatus.FAILED
        assert result.error_code == "TIMEOUT"
        assert result.is_retryable is True
        assert [e.code for e in updates["errors"]] == ["TIMEOUT"]
        assert updates["errors"][0].step_id == "s1"

    def test_executor_exception_becomes_failed_step(self) -> None:
        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            raise RuntimeError("boom")

        node = build_execute_steps_node(executor=executor)
        updates = _invoke(node, _node_state(plan=Plan(steps=[_step()])))
        result = updates["step_results"]["s1"]
        assert result.status == StepStatus.FAILED
        assert result.error_code == "EXECUTION_FAILED"
        assert "boom" in result.error_message
        assert [e.code for e in updates["errors"]] == ["EXECUTION_FAILED"]

    def test_dependency_failure_skips_dependent(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.failure(
                tool_version_id="v1", error_code="TIMEOUT", error_message="timeout"
            )

        node = build_execute_steps_node(executor=executor)
        plan = Plan(steps=[_step("s1"), _step("s2", depends_on=["s1"])])
        state = _node_state(
            plan=plan,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1"], ["s2"]]),
        )
        updates = _invoke(node, state)
        assert calls == ["getProductById"]
        assert updates["step_results"]["s1"].status == StepStatus.FAILED
        assert updates["step_results"]["s2"].status == StepStatus.SKIPPED

    def test_non_allow_policy_skips_step(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _node_state(
            plan=plan,
            decisions={
                "s1": PolicyDecision.ALLOW,
                "s2": PolicyDecision.REQUIRE_APPROVAL,
            },
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1", "s2"]]),
        )
        updates = _invoke(node, state)
        assert calls == ["getProductById"]
        assert updates["step_results"]["s2"].status == StepStatus.SKIPPED

    def test_completed_step_is_not_rerun(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(steps=[_step("s1")])
        existing = {"s1": _completed("s1", {"price": 10})}
        state = _node_state(plan=plan, existing=existing)
        updates = _invoke(node, state)
        assert calls == []
        assert updates["step_results"]["s1"] == existing["s1"]

    def test_resume_reruns_only_incomplete_steps(self) -> None:
        calls: list[str] = []

        async def executor(tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            calls.append(tool_name)
            return ToolResult.success(tool_version_id="v1", data={})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(
            steps=[
                _step("s1"),
                _step("s2", tool_name="getOrderByOrderId", depends_on=["s1"]),
            ]
        )
        existing = {"s1": _completed("s1", {"id": 7})}
        state = _node_state(
            plan=plan,
            existing=existing,
            validation=PlanValidation(is_valid=True, parallel_groups=[["s1"], ["s2"]]),
        )
        updates = _invoke(node, state)
        assert calls == ["getOrderByOrderId"]
        assert updates["step_results"]["s1"] == existing["s1"]
        assert updates["step_results"]["s2"].status == StepStatus.COMPLETED

    def test_preserves_existing_step_results(self) -> None:
        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.success(tool_version_id="v1", data={"price": 10})

        node = build_execute_steps_node(executor=executor)
        plan = Plan(steps=[_step("s1")])
        existing = {"prior": _completed("prior", {"whatever": 1})}
        state = _node_state(plan=plan, existing=existing)
        updates = _invoke(node, state)
        assert updates["step_results"]["prior"] == existing["prior"]
        assert updates["step_results"]["s1"].status == StepStatus.COMPLETED

    def test_preserves_existing_errors(self) -> None:
        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.failure(
                tool_version_id="v1", error_code="TIMEOUT", error_message="timeout"
            )

        node = build_execute_steps_node(executor=executor)
        existing_error = StateError(code="EXISTING", message="x")
        state = _node_state(plan=Plan(steps=[_step()]), errors=[existing_error])
        updates = _invoke(node, state)
        assert [e.code for e in updates["errors"]] == ["EXISTING", "TIMEOUT"]

    def test_missing_plan_appends_no_plan_error(self) -> None:
        async def executor(_tool_name: str, _arguments: dict[str, Any]) -> ToolResult:
            raise AssertionError("executor must not run without a plan")

        node = build_execute_steps_node(executor=executor)
        state = AgentState(run_id="r1", tenant_id="t1", query="q", plan=None)
        updates = _invoke(node, state)
        assert [e.code for e in updates["errors"]] == ["NO_PLAN"]


def test_resolve_arguments_signature_is_callable() -> None:
    # Guards the pure-function seam used by the node so a refactor cannot
    # silently change it into an unexported helper.
    assert callable(resolve_arguments)
