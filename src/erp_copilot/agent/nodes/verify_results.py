"""Deterministic verification node — task 4.11.

Defence layer 4: the semantic gate (docs/03 §5.6). Tool success is not
business success — each completed step's success_condition (when present) is
evaluated against the step's result data. This catches semantic mis-selection
that validate_plan's strict schema cannot: "查苹果却选了 getProductById 传
id=苹果" passes layer 2 (the schema accepts one argument), but the returned
product name fails `response.name == '苹果'` here.

The node classifies the run for the recovery sink:
- no failures → SUCCEEDED (routes to finalize);
- every failing step retryable → RETRYING;
- any permanent failure (non-retryable tool error, missing step result, or a
  success_condition that is invalid / not met) → REPLANNING.

The 9-node topology has a single recover_or_replan sink — the retry vs replan
vs give-up split is task 5.8's job — so 4.11 records the classification in
AgentStatus for the recover node and checkpoint to read. Both RETRYING and
REPLANNING route to recover_or_replan via the existing errors-based edge.
Failures from execute_ready_steps already live in state.errors; this node only
appends errors it discovers itself (success_condition / missing result).

success_condition is a deterministic predicate (the four-layer defence table
marks layer 4 deterministic — an LLM judge would not be reproducible). It is
evaluated with an AST whitelist, never raw eval: the condition string comes
from the LLM-generated plan (untrusted input), so only comparison / boolean /
arithmetic expressions, attribute and subscript access, and len/str/int/float
calls are allowed, and the eval runs with an empty __builtins__.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

from erp_copilot.agent.state import AgentState, AgentStatus, StateError, StepResult
from erp_copilot.domain.enums import StepStatus

# Success-condition evaluation is deliberately confined to these functions.
# Anything else the AST walker does not explicitly allow is rejected, so an
# injected condition like `__import__('os').system(...)` can never run.
_ALLOWED_FUNCTIONS: frozenset[str] = frozenset({"len", "str", "int", "float"})
_ALLOWED_NAMES: frozenset[str] = frozenset({"response", *_ALLOWED_FUNCTIONS})
_RESPONSE_NAME = "response"


class _AttrDict(dict[str, Any]):
    """dict exposing keys as attributes so `response.status` works.

    Normal dict lookup wins over __getattr__ (dict methods like .items stay
    usable), and keys shadowing a dict method are an acceptable edge case for
    tool result data.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return _AttrDict({key: _wrap(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


_COMPARISON_OPS: tuple[type[ast.cmpop], ...] = (
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)
_BINARY_OPS: tuple[type[ast.operator], ...] = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)


def _validate_expression(node: ast.AST) -> bool:
    """Reject any construct outside the success-condition whitelist.

    Operator nodes (ast.Eq, ast.And, ...) are checked explicitly — a blanket
    walk over iter_child_nodes would reject every comparison because the
    operator nodes themselves are not whitelisted.
    """
    if isinstance(node, ast.Expression):
        return _validate_expression(node.body)
    if isinstance(node, ast.BoolOp):
        return isinstance(node.op, (ast.And, ast.Or)) and all(
            _validate_expression(value) for value in node.values
        )
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, _BINARY_OPS) and all(
            _validate_expression(value) for value in (node.left, node.right)
        )
    if isinstance(node, ast.Compare):
        return (
            all(isinstance(comparison_op, _COMPARISON_OPS) for comparison_op in node.ops)
            and _validate_expression(node.left)
            and all(_validate_expression(comparator) for comparator in node.comparators)
        )
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)) and _validate_expression(
            node.operand
        )
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float, str, bool)) or node.value is None
    if isinstance(node, ast.Name):
        return node.id in _ALLOWED_NAMES
    if isinstance(node, ast.Attribute):
        return _validate_expression(node.value)
    if isinstance(node, ast.Subscript):
        slice_ok = isinstance(node.slice, ast.Slice) or _validate_expression(node.slice)
        return slice_ok and _validate_expression(node.value)
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in _ALLOWED_FUNCTIONS
            and not node.keywords
            and all(_validate_expression(arg) for arg in node.args)
        )
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_validate_expression(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        # A None key marks `{**x}` unpacking, which is not whitelisted.
        return all(
            key is not None and _validate_expression(key) and _validate_expression(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    return False


def evaluate_success_condition(
    condition: str,
    data: dict[str, Any] | None,
) -> tuple[bool, StateError | None]:
    """Evaluate one success_condition predicate against step result *data*.

    Returns (True, None) when the predicate holds; (False, None) when it holds
    but evaluates to False (the node turns that into SUCCESS_CONDITION_FAILED);
    (False, StateError) when the condition is malformed or uses a construct
    outside the whitelist (SUCCESS_CONDITION_INVALID — a plan defect) or there
    is no data to judge against (SUCCESS_CONDITION_FAILED — the goal cannot be
    proven, so it is not met).
    """
    if data is None:
        return False, StateError(
            code="SUCCESS_CONDITION_FAILED",
            message="success_condition 无法求值：步骤没有返回数据",
            details={"condition": condition, "reason": "no data"},
        )
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError:
        return False, StateError(
            code="SUCCESS_CONDITION_INVALID",
            message="success_condition 语法错误",
            details={"condition": condition},
        )
    if not _validate_expression(tree):
        return False, StateError(
            code="SUCCESS_CONDITION_INVALID",
            message="success_condition 含不允许的表达式结构",
            details={"condition": condition},
        )
    namespace: dict[str, Any] = {
        _RESPONSE_NAME: _wrap(data),
        "len": len,
        "str": str,
        "int": int,
        "float": float,
    }
    try:
        result = eval(compile(tree, "<success_condition>", "eval"), {"__builtins__": {}}, namespace)
    except (AttributeError, IndexError, KeyError, TypeError, ZeroDivisionError) as exc:
        # The condition referenced data that is not there (e.g. the tool
        # returned {"product_name": ...} but the condition reads
        # response.name) — the exact semantic mismatch layer 4 exists to
        # catch, so it is a verification failure, not a crash.
        return False, StateError(
            code="SUCCESS_CONDITION_FAILED",
            message="success_condition 求值失败：返回数据不满足条件引用",
            details={"condition": condition, "reason": str(exc)},
        )
    return bool(result), None


def _verify_step(
    step_id: str,
    result: StepResult,
    condition: str | None,
) -> tuple[bool, bool, list[StateError]]:
    """Classify one step: returns (retryable, permanent, new_errors).

    SKIPPED is not a verification failure (policy-gated steps are the Phase 5
    approval flow's concern); a FAILED step's retryability decides its class —
    execute_ready_steps already recorded the error, so nothing is appended
    here.
    """
    if result.status == StepStatus.COMPLETED:
        if condition is None:
            return False, False, []
        satisfied, error = evaluate_success_condition(condition, result.data)
        if error is not None:
            return False, True, [error]
        if not satisfied:
            return (
                False,
                True,
                [
                    StateError(
                        code="SUCCESS_CONDITION_FAILED",
                        message="步骤执行成功，但返回未满足 success_condition",
                        step_id=step_id,
                        details={"condition": condition},
                    )
                ],
            )
        return False, False, []
    if result.status == StepStatus.FAILED:
        return result.is_retryable, not result.is_retryable, []
    return False, False, []


def build_verify_results_node() -> Callable[[AgentState], dict[str, Any]]:
    """Build the verify_results LangGraph node (no injected dependencies — the
    whole node is deterministic)."""

    def verify_results_node(state: AgentState) -> dict[str, Any]:
        if state.plan is None:
            error = StateError(
                code="NO_PLAN",
                message="verify_results 运行时没有可验证的 plan",
            )
            return {"errors": [*state.errors, error], "status": AgentStatus.FAILED}

        new_errors: list[StateError] = []
        retryable = False
        permanent = False
        for step in state.plan.steps:
            result = state.step_results.get(step.step_id)
            if result is None:
                permanent = True
                new_errors.append(
                    StateError(
                        code="MISSING_STEP_RESULT",
                        message=f"步骤 {step.step_id} 没有执行结果，无法验证",
                        step_id=step.step_id,
                    )
                )
                continue
            step_retryable, step_permanent, step_errors = _verify_step(
                step.step_id, result, step.success_condition
            )
            retryable = retryable or step_retryable
            permanent = permanent or step_permanent
            new_errors.extend(step_errors)

        if permanent:
            status = AgentStatus.REPLANNING
        elif retryable:
            status = AgentStatus.RETRYING
        else:
            status = AgentStatus.SUCCEEDED
        updates: dict[str, Any] = {"status": status}
        if new_errors:
            updates["errors"] = [*state.errors, *new_errors]
        return updates

    return verify_results_node
