"""Unit tests for the verify_results node — task 4.11.

Defence layer 4: the semantic gate. Tool success is not business success —
each completed step's success_condition (when present) is evaluated against
its result data, catching semantic mis-selection that validate_plan's strict
schema cannot (e.g. "查苹果却传了 id=苹果": getProductById succeeds, but the
returned product name fails `response.name == '苹果'`).

The node classifies the run for the recovery sink: all steps ok -> SUCCEEDED
(finalize); any failing step retryable -> RETRYING; any permanent failure
(non-retryable tool error, or success_condition not met) -> REPLANNING. The
9-node topology has a single recover_or_replan sink — the retry/replan/give-up
split is task 5.8's job, so 4.11 records the classification in AgentStatus.

success_condition is a deterministic predicate evaluated with an AST
whitelist — never raw eval of the LLM-supplied string (code injection).
"""

from __future__ import annotations

from typing import Any

from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.nodes.verify_results import (
    build_verify_results_node,
    evaluate_success_condition,
)
from erp_copilot.agent.planner import build_plan_from_intent
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    Plan,
    PlanStep,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel


def _step(step_id: str = "s1", **overrides: object) -> PlanStep:
    data: dict[str, object] = {
        "step_id": step_id,
        "tool_name": "getProductById",
        "risk_level": ToolRiskLevel.READ,
        "depends_on": [],
    }
    data.update(overrides)
    return PlanStep(**data)


def _completed(step_id: str, data: dict[str, Any] | None = None) -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.COMPLETED, data=data)


def _failed(step_id: str, *, is_retryable: bool) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.FAILED,
        error_code="TIMEOUT" if is_retryable else "PERMISSION_DENIED",
        is_retryable=is_retryable,
    )


def _skipped(step_id: str) -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.SKIPPED)


def _node_state(
    *,
    plan: Plan | None,
    results: dict[str, StepResult] | None = None,
    errors: list[StateError] | None = None,
) -> AgentState:
    return AgentState(
        run_id="r1",
        tenant_id="t1",
        user_id="u1",
        query="q",
        plan=plan,
        step_results=results or {},
        errors=errors or [],
    )


def _invoke(state: AgentState) -> dict[str, Any]:
    return build_verify_results_node()(state)


class TestEvaluateSuccessCondition:
    def test_returns_true_for_comparison(self) -> None:
        ok, error = evaluate_success_condition("response.status == 'ok'", {"status": "ok"})
        assert error is None
        assert ok is True

    def test_len_call_against_list(self) -> None:
        ok, error = evaluate_success_condition(
            "len(response.suppliers) > 0", {"suppliers": ["a", "b"]}
        )
        assert error is None
        assert ok is True

    def test_len_call_false_on_empty(self) -> None:
        ok, error = evaluate_success_condition("len(response.suppliers) > 0", {"suppliers": []})
        assert error is None
        assert ok is False

    def test_boolean_composition(self) -> None:
        condition = "response.status == 'ok' and len(response.suppliers) > 0"
        ok, error = evaluate_success_condition(condition, {"status": "ok", "suppliers": ["a"]})
        assert ok is True
        ok, error = evaluate_success_condition(condition, {"status": "error", "suppliers": ["a"]})
        assert ok is False

    def test_subscript_access_works_like_attribute(self) -> None:
        ok, error = evaluate_success_condition("response['status'] == 'ok'", {"status": "ok"})
        assert error is None
        assert ok is True

    def test_numeric_comparison(self) -> None:
        ok, error = evaluate_success_condition("response.price < 100", {"price": 99})
        assert error is None
        assert ok is True

    def test_unicode_literal(self) -> None:
        ok, error = evaluate_success_condition("response.name == '苹果'", {"name": "苹果"})
        assert error is None
        assert ok is True

    def test_malformed_syntax_is_invalid(self) -> None:
        ok, error = evaluate_success_condition("response.status ==", {"status": "ok"})
        assert ok is False
        assert error is not None
        assert error.code == "SUCCESS_CONDITION_INVALID"

    def test_import_is_rejected(self) -> None:
        ok, error = evaluate_success_condition("__import__('os')", {})
        assert ok is False
        assert error is not None
        assert error.code == "SUCCESS_CONDITION_INVALID"

    def test_unknown_name_is_rejected(self) -> None:
        ok, error = evaluate_success_condition("os.system('rm -rf /')", {})
        assert ok is False
        assert error is not None
        assert error.code == "SUCCESS_CONDITION_INVALID"

    def test_call_to_disallowed_function_is_rejected(self) -> None:
        ok, error = evaluate_success_condition("response.suppliers()", {"suppliers": []})
        assert ok is False
        assert error is not None
        assert error.code == "SUCCESS_CONDITION_INVALID"

    def test_missing_data_cannot_satisfy_condition(self) -> None:
        ok, error = evaluate_success_condition("response.status == 'ok'", None)
        assert ok is False
        assert error is not None
        assert error.code == "SUCCESS_CONDITION_FAILED"

    def test_missing_key_is_failed_not_crash(self) -> None:
        ok, error = evaluate_success_condition("response.name == '苹果'", {"product_name": "苹果"})
        assert ok is False
        assert error is not None
        assert error.code == "SUCCESS_CONDITION_FAILED"


class TestBuildVerifyResultsNode:
    def test_all_completed_without_condition_succeeds(self) -> None:
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _node_state(plan=plan, results={"s1": _completed("s1"), "s2": _completed("s2")})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.SUCCEEDED

    def test_success_condition_met_succeeds(self) -> None:
        plan = Plan(steps=[_step("s1", success_condition="response.status == 'ok'")])
        state = _node_state(plan=plan, results={"s1": _completed("s1", {"status": "ok"})})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.SUCCEEDED

    def test_success_condition_not_met_is_permanent_failure(self) -> None:
        plan = Plan(steps=[_step("s1", success_condition="response.status == 'ok'")])
        state = _node_state(plan=plan, results={"s1": _completed("s1", {"status": "error"})})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.REPLANNING
        errors = updates["errors"]
        assert [e.code for e in errors] == ["SUCCESS_CONDITION_FAILED"]
        assert errors[0].step_id == "s1"
        assert errors[0].details["condition"] == "response.status == 'ok'"

    def test_invalid_success_condition_is_permanent_failure(self) -> None:
        plan = Plan(steps=[_step("s1", success_condition="response.status ==")])
        state = _node_state(plan=plan, results={"s1": _completed("s1", {"status": "ok"})})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.REPLANNING
        assert [e.code for e in updates["errors"]] == ["SUCCESS_CONDITION_INVALID"]

    def test_condition_referencing_missing_key_is_permanent_failure(self) -> None:
        plan = Plan(steps=[_step("s1", success_condition="response.name == '苹果'")])
        state = _node_state(plan=plan, results={"s1": _completed("s1", {"product_name": "苹果"})})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.REPLANNING
        assert [e.code for e in updates["errors"]] == ["SUCCESS_CONDITION_FAILED"]

    def test_retryable_failure_routes_to_retry(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _node_state(
            plan=plan,
            results={"s1": _failed("s1", is_retryable=True)},
            errors=[StateError(code="TIMEOUT", message="timeout", step_id="s1")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.RETRYING
        assert "errors" not in updates

    def test_permanent_failure_routes_to_replan(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _node_state(
            plan=plan,
            results={"s1": _failed("s1", is_retryable=False)},
            errors=[StateError(code="PERMISSION_DENIED", message="denied", step_id="s1")],
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.REPLANNING
        assert "errors" not in updates

    def test_permanent_failure_wins_over_retryable(self) -> None:
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _node_state(
            plan=plan,
            results={
                "s1": _failed("s1", is_retryable=True),
                "s2": _failed("s2", is_retryable=False),
            },
        )
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.REPLANNING

    def test_skipped_steps_do_not_fail_verification(self) -> None:
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _node_state(plan=plan, results={"s1": _completed("s1"), "s2": _skipped("s2")})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.SUCCEEDED

    def test_missing_step_result_is_permanent_failure(self) -> None:
        plan = Plan(steps=[_step("s1"), _step("s2")])
        state = _node_state(plan=plan, results={"s1": _completed("s1")})
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.REPLANNING
        errors = updates["errors"]
        assert [e.code for e in errors] == ["MISSING_STEP_RESULT"]
        assert errors[0].step_id == "s2"

    def test_missing_plan_appends_no_plan_error(self) -> None:
        state = _node_state(plan=None)
        updates = _invoke(state)
        assert updates["status"] == AgentStatus.FAILED
        assert [e.code for e in updates["errors"]] == ["NO_PLAN"]

    def test_preserves_existing_errors(self) -> None:
        plan = Plan(steps=[_step("s1", success_condition="response.status == 'ok'")])
        existing = StateError(code="EXISTING", message="x")
        state = _node_state(
            plan=plan,
            results={"s1": _completed("s1", {"status": "error"})},
            errors=[existing],
        )
        updates = _invoke(state)
        assert [e.code for e in updates["errors"]] == ["EXISTING", "SUCCESS_CONDITION_FAILED"]

    def test_successful_step_keeps_its_result_data(self) -> None:
        plan = Plan(steps=[_step("s1")])
        state = _node_state(plan=plan, results={"s1": _completed("s1", {"price": 10})})
        updates = _invoke(state)
        assert "step_results" not in updates


def test_evaluate_success_condition_signature_is_callable() -> None:
    # Guards the pure-function seam used by the node so a refactor cannot
    # silently change it into an unexported helper.
    assert callable(evaluate_success_condition)


class TestDeterministicPlanSemanticGate:
    """The deterministic planner's success_conditions are enforced by verify —
    proves defence layer 4 is active on the worker production path.

    Feeds executor-shaped result data (the shapes apps/worker/executor.py
    produces) through the real classify_intent → build_plan_from_intent → verify
    chain. Before the planner sets success_condition, both cases SUCCEEDED
    because verify lazily skipped condition-less completed steps.
    """

    @staticmethod
    def _create_plan() -> Plan:
        intent = classify_intent("帮我在上海下一单 10 KG 苹果")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        return plan

    def _state(
        self,
        s1_data: dict[str, Any],
        s3_data: dict[str, Any],
    ) -> AgentState:
        plan = self._create_plan()
        results: dict[str, StepResult] = {
            "s1": _completed("s1", s1_data),
            "s2": _completed("s2", {"suppliers": [{"supplier_id": 3}], "supplier_id": 3}),
            "s3": _completed("s3", s3_data),
        }
        return _node_state(plan=plan, results=results)

    def test_matching_executor_data_succeeds(self) -> None:
        updates = _invoke(
            self._state(
                s1_data={"product_id": 1, "name": "苹果", "price": 10.0, "stock": 90, "unit": "KG"},
                s3_data={
                    "order_id": "a1b2c3d4e5f6",
                    "product_id": 1,
                    "quantity": 10,
                    "supplier_id": 3,
                    "region": "上海",
                    "amount": 100.0,
                    "status": "CREATED",
                    "idempotency_key": "r1:s3",
                    "created_at": "2026-08-17T00:00:00Z",
                },
            )
        )
        assert updates["status"] == AgentStatus.SUCCEEDED

    def test_wrong_product_name_triggers_replanning(self) -> None:
        # The tool ran and the step COMPLETED, but it returned the wrong
        # product — the semantic mismatch layer 4 exists to catch.
        updates = _invoke(
            self._state(
                s1_data={"product_id": 2, "name": "香蕉", "price": 8.0, "stock": 50, "unit": "KG"},
                s3_data={
                    "order_id": "a1b2c3d4e5f6",
                    "product_id": 2,
                    "amount": 80.0,
                    "status": "CREATED",
                },
            )
        )
        assert updates["status"] == AgentStatus.REPLANNING
        errors = updates["errors"]
        assert [e.code for e in errors] == ["SUCCESS_CONDITION_FAILED"]
        assert errors[0].step_id == "s1"
        assert errors[0].details["condition"] == "response.name == '苹果'"
