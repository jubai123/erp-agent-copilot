"""Unit tests for saga compensation — 缺口二.

When a multi-write Plan DAG partially succeeds then fails permanently at
give-up time, the completed earlier writes leave the business dangling
(modify = s1 lookup → s2 cancelOrder → s3 createOrder: if s3 fails forever,
s2 already cancelled the original order with no replacement). The compensation
engine detects the partial write and a dedicated graph node runs the
compensating actions (cancelOrder ↔ createOrder, addProduct → removeProductById,
addSuppliers → deleteSupplierById) before finalize.

Safety invariants under test:
- compensation only fires for NON-ambiguous write failures — TIMEOUT /
  UPSTREAM_5xx / UPSTREAM_UNAVAILABLE give up for a human instead, because
  re-running an opposite write over an unknown-outcome write risks a double
  effect;
- the run stays FAILED (compensation supplements, never replaces, the human
  reconciliation path) and errors[0] — persist_run's failure_code — is
  preserved: compensation errors are appended after it;
- compensating writes go through the same at-most-once executor path, keyed
  {run_id}:comp:{step_id}.
"""

from __future__ import annotations

import asyncio
from typing import Any

from erp_copilot.agent.compensation import (
    PARTIAL_WRITE_DETECTED,
    WRITE_PARTIAL_COMPENSATED,
    WRITE_PARTIAL_UNCOMPENSATED,
    build_compensate_partial_writes_node,
    build_compensation_steps,
    detect_partial_writes,
    is_ambiguous_code,
)
from erp_copilot.agent.graph import build_agent_graph
from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node
from erp_copilot.agent.nodes.policy_check import build_policy_check_node
from erp_copilot.agent.nodes.recover_or_replan import MAX_REPLANS, MAX_RETRIES
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


def _step(
    step_id: str,
    *,
    tool_name: str,
    risk: ToolRiskLevel = ToolRiskLevel.READ,
    depends_on: list[str] | None = None,
    arguments: dict[str, Any] | None = None,
    argument_sources: dict[str, str] | None = None,
    fallback: str | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool_name=tool_name,
        risk_level=risk,
        depends_on=depends_on or [],
        arguments=arguments or {},
        argument_sources=argument_sources or {},
        fallback=fallback,
    )


def _completed(step_id: str, data: dict[str, Any] | None = None) -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.COMPLETED, data=data)


def _failed_permanent(step_id: str, code: str = "INSUFFICIENT_STOCK") -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.FAILED,
        error_code=code,
        error_message=code,
        is_retryable=False,
    )


def _modify_plan() -> Plan:
    """s1 lookup → s2 cancelOrder → s3 createOrder (the only current write DAG)."""
    return Plan(
        steps=[
            _step("s1", tool_name="getOrderByOrderId", arguments={"order_id": "ORD-100"}),
            _step(
                "s2",
                tool_name="cancelOrder",
                risk=ToolRiskLevel.WRITE,
                depends_on=["s1"],
                arguments={"idempotency_key": "r1:s2"},
                argument_sources={"order_id": "step:s1"},
                fallback="重新激活原订单",
            ),
            _step(
                "s3",
                tool_name="createOrder",
                risk=ToolRiskLevel.WRITE,
                depends_on=["s1", "s2"],
                arguments={"quantity": 5, "idempotency_key": "r1:s3"},
                argument_sources={
                    "product_id": "step:s1",
                    "supplier_id": "step:s1",
                    "region": "step:s1",
                },
                fallback="取消新建订单以补偿",
            ),
        ]
    )


def _cancelled_order() -> dict[str, Any]:
    return {
        "order_id": "ORD-100",
        "product_id": 10094,
        "product_name": "苹果",
        "quantity": 5,
        "supplier_id": 222,
        "region": "上海",
        "amount": 100.0,
        "status": "CANCELLED",
        "idempotency_key": "r1:s2",
        "created_at": "2026-08-22T10:00:00",
    }


class TestDetectPartialWrites:
    def test_modify_dag_partial_failure_returns_completed_write(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _completed("s1", _cancelled_order()),
            "s2": _completed("s2", _cancelled_order()),
            "s3": _failed_permanent("s3"),
        }
        assert detect_partial_writes(plan, results) == ["s2"]

    def test_reverse_topological_order(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    "s1",
                    tool_name="createOrder",
                    risk=ToolRiskLevel.WRITE,
                    arguments={"idempotency_key": "r1:s1"},
                    fallback="取消订单",
                ),
                _step(
                    "s2",
                    tool_name="addProduct",
                    risk=ToolRiskLevel.WRITE,
                    arguments={"idempotency_key": "r1:s2"},
                    fallback="删除商品",
                ),
                _step(
                    "s3",
                    tool_name="createOrder",
                    risk=ToolRiskLevel.WRITE,
                    depends_on=["s1", "s2"],
                    arguments={"idempotency_key": "r1:s3"},
                    fallback="取消订单",
                ),
            ]
        )
        results = {
            "s1": _completed("s1", {"order_id": "O-1"}),
            "s2": _completed("s2", {"product_id": 7}),
            "s3": _failed_permanent("s3"),
        }
        # Most recently executed write compensates first (reverse topo).
        assert detect_partial_writes(plan, results) == ["s2", "s1"]

    def test_ambiguous_write_failure_never_triggers(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _completed("s1", _cancelled_order()),
            "s2": _completed("s2", _cancelled_order()),
            "s3": StepResult(
                step_id="s3",
                status=StepStatus.FAILED,
                error_code="TIMEOUT",
                is_retryable=True,
            ),
        }
        assert detect_partial_writes(plan, results) == []

    def test_failed_read_does_not_trigger(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _failed_permanent("s1"),
            "s2": _completed("s2", _cancelled_order()),
        }
        assert detect_partial_writes(plan, results) == []

    def test_no_earlier_completed_write_returns_empty(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    "s1",
                    tool_name="createOrder",
                    risk=ToolRiskLevel.WRITE,
                    arguments={"idempotency_key": "r1:s1"},
                    fallback="取消订单",
                )
            ]
        )
        results = {"s1": _failed_permanent("s1")}
        assert detect_partial_writes(plan, results) == []

    def test_all_completed_returns_empty(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _completed("s1", _cancelled_order()),
            "s2": _completed("s2", _cancelled_order()),
            "s3": _completed("s3", {"order_id": "ORD-101"}),
        }
        assert detect_partial_writes(plan, results) == []

    def test_none_plan_returns_empty(self) -> None:
        assert detect_partial_writes(None, {}) == []


class TestIsAmbiguousCode:
    def test_known_transient_codes(self) -> None:
        assert is_ambiguous_code("TIMEOUT")
        assert is_ambiguous_code("UPSTREAM_UNAVAILABLE")

    def test_upstream_5xx(self) -> None:
        assert is_ambiguous_code("UPSTREAM_500")
        assert is_ambiguous_code("UPSTREAM_503")
        assert not is_ambiguous_code("UPSTREAM_404")

    def test_permanent_and_empty(self) -> None:
        assert not is_ambiguous_code("INSUFFICIENT_STOCK")
        assert not is_ambiguous_code(None)


class TestBuildCompensationSteps:
    def test_cancel_order_recreates_order_from_result_fields(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _completed("s1", _cancelled_order()),
            "s2": _completed("s2", _cancelled_order()),
            "s3": _failed_permanent("s3"),
        }
        actions = build_compensation_steps(plan, results)
        assert len(actions) == 1
        action = actions[0]
        assert action.step_id == "s2"
        assert action.tool_name == "createOrder"
        assert action.arguments == {
            "product_id": 10094,
            "quantity": 5,
            "supplier_id": 222,
            "region": "上海",
        }

    def test_create_order_compensates_with_cancel(self) -> None:
        plan = Plan(
            steps=[
                _step(
                    "s1",
                    tool_name="createOrder",
                    risk=ToolRiskLevel.WRITE,
                    arguments={"idempotency_key": "r1:s1"},
                    fallback="取消订单",
                ),
                _step(
                    "s2",
                    tool_name="updateOrderStatus",
                    risk=ToolRiskLevel.WRITE,
                    depends_on=["s1"],
                    arguments={"idempotency_key": "r1:s2"},
                    fallback="回滚状态",
                ),
            ]
        )
        results = {
            "s1": _completed("s1", {"order_id": "ORD-200"}),
            "s2": _failed_permanent("s2", "INVALID_STATUS_TRANSITION"),
        }
        actions = build_compensation_steps(plan, results)
        assert len(actions) == 1
        assert actions[0].tool_name == "cancelOrder"
        assert actions[0].arguments == {"order_id": "ORD-200"}

    def test_non_compensatable_write_is_excluded(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _completed("s1", _cancelled_order()),
            # updateOrderStatus needs the pre-write value to roll back — not in
            # the registry, so it is excluded and reported back by the node.
            "s2": _completed("s2", _cancelled_order()),
            "s3": _failed_permanent("s3"),
        }
        # Swap s2 to a non-compensatable tool.
        plan = plan.model_copy(
            update={
                "steps": [
                    plan.steps[0],
                    _step(
                        "s2",
                        tool_name="updateOrderStatus",
                        risk=ToolRiskLevel.WRITE,
                        depends_on=["s1"],
                        arguments={"idempotency_key": "r1:s2"},
                        argument_sources={"order_id": "step:s1"},
                        fallback="回滚状态",
                    ),
                    plan.steps[2],
                ]
            }
        )
        assert build_compensation_steps(plan, results) == []

    def test_missing_source_field_is_excluded(self) -> None:
        plan = _modify_plan()
        results = {
            "s1": _completed("s1", _cancelled_order()),
            # cancelOrder result without the order's business fields cannot be
            # re-created.
            "s2": _completed("s2", {"order_id": "ORD-100"}),
            "s3": _failed_permanent("s3"),
        }
        assert build_compensation_steps(plan, results) == []


def _node_state(
    *,
    plan: Plan | None = None,
    step_results: dict[str, StepResult] | None = None,
    compensation_pending: bool = True,
) -> AgentState:
    return AgentState(
        run_id="r1",
        tenant_id="t1",
        user_id="u1",
        query="q",
        status=AgentStatus.FAILED,
        plan=plan,
        step_results=step_results or {},
        compensation_pending=compensation_pending,
    )


class TestCompensateNode:
    async def _compensate(
        self,
        executor: Any,
        *,
        state: AgentState,
    ) -> dict[str, Any]:
        node = build_compensate_partial_writes_node(executor=executor)
        return await node(state)

    def test_noop_when_not_pending(self) -> None:
        updates = asyncio.run(self._compensate(None, state=_node_state(compensation_pending=False)))
        assert updates == {}

    def test_compensates_completed_write(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        async def executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            calls.append((tool_name, dict(arguments)))
            return ToolResult.success(
                tool_version_id="v1", data={"order_id": "ORD-101", "amount": 100.0}
            )

        state = _node_state(
            plan=_modify_plan(),
            step_results={
                "s1": _completed("s1", _cancelled_order()),
                "s2": _completed("s2", _cancelled_order()),
                "s3": _failed_permanent("s3"),
            },
        )
        updates = asyncio.run(self._compensate(executor, state=state))
        assert updates["compensation_pending"] is False
        assert updates["step_results"]["comp-s2"].status == StepStatus.COMPLETED
        codes = [e.code for e in updates["errors"]]
        assert WRITE_PARTIAL_COMPENSATED in codes
        # The compensating write went through the at-most-once keyed path.
        comp_calls = [
            args for tool, args in calls if args.get("idempotency_key") == "r1:comp:s2"
        ]
        assert len(comp_calls) == 1
        assert comp_calls[0]["product_id"] == 10094

    def test_non_compensatable_write_reported_uncompensated(self) -> None:
        async def executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            raise AssertionError("compensation should not run for a non-compensatable write")

        modify = _modify_plan()
        plan = modify.model_copy(
            update={
                "steps": [
                    modify.steps[0],
                    _step(
                        "s2",
                        tool_name="updateOrderStatus",
                        risk=ToolRiskLevel.WRITE,
                        depends_on=["s1"],
                        arguments={"idempotency_key": "r1:s2"},
                        argument_sources={"order_id": "step:s1"},
                        fallback="回滚状态",
                    ),
                    modify.steps[2],
                ]
            }
        )
        state = _node_state(
            plan=plan,
            step_results={
                "s1": _completed("s1", _cancelled_order()),
                "s2": _completed("s2", _cancelled_order()),
                "s3": _failed_permanent("s3"),
            },
        )
        updates = asyncio.run(self._compensate(executor, state=state))
        assert updates["compensation_pending"] is False
        codes = [e.code for e in updates["errors"]]
        assert WRITE_PARTIAL_UNCOMPENSATED in codes
        assert codes[-1] == WRITE_PARTIAL_UNCOMPENSATED

    def test_failed_compensation_reported_uncompensated(self) -> None:
        async def executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            return ToolResult.failure(
                tool_version_id="v1",
                error_code="ORDER_CREATE_FAILED",
                error_message="create failed",
                is_retryable=False,
            )

        state = _node_state(
            plan=_modify_plan(),
            step_results={
                "s1": _completed("s1", _cancelled_order()),
                "s2": _completed("s2", _cancelled_order()),
                "s3": _failed_permanent("s3"),
            },
        )
        updates = asyncio.run(self._compensate(executor, state=state))
        assert updates["compensation_pending"] is False
        assert updates["step_results"]["comp-s2"].status == StepStatus.FAILED
        assert updates["errors"][-1].code == WRITE_PARTIAL_UNCOMPENSATED


class TestGraphClosure:
    """Real graph: recover gives up on budget exhaustion → compensation fires
    → finalize. The run stays FAILED and the original failure_code (errors[0])
    is preserved — compensation errors are appended after it."""

    def test_modify_dag_partial_failure_compensates_then_finalizes(self) -> None:
        run_id = "run-modify"
        cancelled = {
            "order_id": "ORD-100",
            "product_id": 10094,
            "product_name": "苹果",
            "quantity": 5,
            "supplier_id": 222,
            "region": "上海",
            "amount": 100.0,
            "status": "CANCELLED",
            "idempotency_key": f"{run_id}:s2",
            "created_at": "2026-08-22T10:00:00",
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        async def fake_executor(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            calls.append((tool_name, dict(arguments)))
            if tool_name == "getOrderByOrderId":
                return ToolResult.success(tool_version_id="v1", data=cancelled)
            if tool_name == "cancelOrder":
                return ToolResult.success(tool_version_id="v1", data=cancelled)
            if tool_name == "createOrder":
                if arguments.get("idempotency_key") == f"{run_id}:comp:s2":
                    return ToolResult.success(
                        tool_version_id="v1",
                        data={**cancelled, "order_id": "ORD-101", "status": "CREATED"},
                    )
                return ToolResult.failure(
                    tool_version_id="v1",
                    error_code="INSUFFICIENT_STOCK",
                    error_message="Insufficient stock",
                    is_retryable=False,
                )
            raise AssertionError(f"unexpected tool {tool_name}")

        plan = Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="getOrderByOrderId",
                    description="查询原订单",
                    arguments={"order_id": "ORD-100"},
                    required_scope="order:read",
                ),
                PlanStep(
                    step_id="s2",
                    tool_name="cancelOrder",
                    description="取消原订单",
                    arguments={"idempotency_key": f"{run_id}:s2"},
                    argument_sources={"order_id": "step:s1"},
                    depends_on=["s1"],
                    risk_level=ToolRiskLevel.WRITE,
                    required_scope="order:write",
                    fallback="重新激活原订单",
                ),
                PlanStep(
                    step_id="s3",
                    tool_name="createOrder",
                    description="按原订单字段重建订单",
                    arguments={"quantity": 5, "idempotency_key": f"{run_id}:s3"},
                    argument_sources={
                        "product_id": "step:s1",
                        "supplier_id": "step:s1",
                        "region": "step:s1",
                    },
                    depends_on=["s1", "s2"],
                    risk_level=ToolRiskLevel.WRITE,
                    required_scope="order:write",
                    fallback="取消新建订单以补偿",
                ),
            ]
        )
        graph = build_agent_graph(
            validate_node=build_validate_plan_node(
                tool_schemas={
                    "getOrderByOrderId": ToolSpec(
                        name="getOrderByOrderId", required_params=["order_id"]
                    ),
                    "cancelOrder": ToolSpec(name="cancelOrder", required_params=["order_id"]),
                    "createOrder": ToolSpec(
                        name="createOrder",
                        required_params=["product_id", "supplier_id", "quantity", "region"],
                    ),
                }
            ),
            policy_node=build_policy_check_node(
                get_scopes=lambda _t, _u: {"order:read", "order:write"}
            ),
            execute_node=build_execute_steps_node(executor=fake_executor),
            verify_node=build_verify_results_node(),
            compensate_node=build_compensate_partial_writes_node(executor=fake_executor),
        )
        result = asyncio.run(
            graph.ainvoke(
                {
                    "run_id": run_id,
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "把订单改成 5 公斤",
                    "plan": plan,
                    "replan_count": MAX_REPLANS,
                    "approvals": [
                        ApprovalRequest(
                            step_id="s2",
                            tool_name="cancelOrder",
                            risk_level=ToolRiskLevel.WRITE,
                            status=ApprovalStatus.APPROVED,
                        ),
                        ApprovalRequest(
                            step_id="s3",
                            tool_name="createOrder",
                            risk_level=ToolRiskLevel.WRITE,
                            status=ApprovalStatus.APPROVED,
                        ),
                    ],
                }
            )
        )
        assert result["status"] == AgentStatus.FAILED
        assert result["compensation_pending"] is False
        codes = [e.code for e in result["errors"]]
        assert PARTIAL_WRITE_DETECTED in codes
        assert WRITE_PARTIAL_COMPENSATED in codes
        # failure_code preservation: errors[0] stays s3's permanent failure.
        assert result["errors"][0].code == "INSUFFICIENT_STOCK"
        assert result["step_results"]["comp-s2"].status == StepStatus.COMPLETED
        comp_calls = [
            args
            for tool, args in calls
            if tool == "createOrder" and args.get("idempotency_key") == f"{run_id}:comp:s2"
        ]
        assert len(comp_calls) == 1
        assert comp_calls[0]["product_id"] == 10094
        assert comp_calls[0]["quantity"] == 5
        assert comp_calls[0]["supplier_id"] == 222
        assert comp_calls[0]["region"] == "上海"


class TestRecoverWiring:
    """recover_or_replan sets compensation_pending only at give-up, never on the
    ambiguous-write / human-intervention branches."""

    def _state(self, **overrides: Any) -> AgentState:
        base: dict[str, Any] = {
            "run_id": "r1",
            "tenant_id": "t1",
            "user_id": "u1",
            "query": "q",
            "status": AgentStatus.REPLANNING,
            "plan": _modify_plan(),
            "step_results": {
                "s1": _completed("s1", _cancelled_order()),
                "s2": _completed("s2", _cancelled_order()),
                "s3": _failed_permanent("s3"),
            },
            "replan_count": MAX_REPLANS,
        }
        base.update(overrides)
        return AgentState(**base)

    def test_budget_exhausted_sets_compensation_pending(self) -> None:
        from erp_copilot.agent.nodes.recover_or_replan import recover_or_replan

        updates = recover_or_replan(
            self._state(
                errors=[StateError(code="INSUFFICIENT_STOCK", message="stock", step_id="s3")]
            )
        )
        assert updates["compensation_pending"] is True
        assert updates["errors"][-1].code == PARTIAL_WRITE_DETECTED
        # failure_code preservation: errors[0] (persist_run's failure_code)
        # stays s3's permanent failure, budget code follows it.
        assert updates["errors"][0].code == "INSUFFICIENT_STOCK"
        assert updates["errors"][1].code == "REPLAN_BUDGET_EXHAUSTED"

    def test_ambiguous_write_give_up_never_compensates(self) -> None:
        from erp_copilot.agent.nodes.recover_or_replan import recover_or_replan

        updates = recover_or_replan(
            self._state(
                step_results={
                    "s1": _completed("s1", _cancelled_order()),
                    "s2": _completed("s2", _cancelled_order()),
                    "s3": StepResult(
                        step_id="s3",
                        status=StepStatus.FAILED,
                        error_code="TIMEOUT",
                        is_retryable=True,
                    ),
                }
            )
        )
        assert "compensation_pending" not in updates
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "WRITE_OUTCOME_AMBIGUOUS"

    def test_read_only_failure_never_compensates(self) -> None:
        from erp_copilot.agent.nodes.recover_or_replan import recover_or_replan

        plan = Plan(steps=[_step("s1", tool_name="getProductById")])
        updates = recover_or_replan(
            self._state(
                status=AgentStatus.RETRYING,
                plan=plan,
                step_results={"s1": _failed_permanent("s1", "PERMISSION_DENIED")},
                retry_count=MAX_RETRIES,
                replan_count=0,
            )
        )
        assert "compensation_pending" not in updates
        assert updates["status"] == AgentStatus.FAILED
        assert updates["errors"][-1].code == "RETRY_BUDGET_EXHAUSTED"
