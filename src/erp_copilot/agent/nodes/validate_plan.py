"""Deterministic Plan DAG validator node — task 4.7.

Defence layer 2: a deterministic gate on the LLM's Plan DAG (docs/03 section
5.6). It rejects cycles, unknown tools, tools outside the intent-filtered
candidate set, missing required arguments, argument_sources that reference
steps not in depends_on, and WRITE steps without a compensation/fallback note.
It also computes the topological order and the parallelisable step groups
(READ steps group together; WRITE steps run serially) for the executor.

Tool schemas are injected as pure data (:class:`ToolSpec`) so the validator is
unit-testable without a database; the app builds them from the tool registry.

argument_sources follow a simple contract: a value of "step:{step_id}" means
the argument's value comes from that step's output and the step must appear in
depends_on. The arguments side is guarded too: any value shaped like a
cross-step reference (from_step_1, $1, 上一步, ...) but not in the canonical
step:{id} form is an UNRESOLVED_REFERENCE — it would otherwise be sent to the
tool verbatim. Semantic misselection (getProductById with id="苹果") passes here
by design — the strict PlanStep schema accepts it — and is caught by
verify_results (layer 4).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from erp_copilot.agent.planner import WRITE_TOOLS
from erp_copilot.agent.state import AgentState, Plan, PlanValidation, StateError
from erp_copilot.domain.enums import ToolRiskLevel

_STEP_SOURCE_PREFIX = "step:"

# Canonical cross-step reference form: "step:{id}" in argument_sources (plus the
# step in depends_on). Only this form is resolvable at execution time.
_CANONICAL_REF_RE = re.compile(r"^step:[A-Za-z0-9]+$")
# Non-canonical reference *family*: placeholder shapes LLMs emit when they mean
# "reuse an earlier step's output" but skip the canonical form (from_step_1, $1,
# 上一步, previous_output, step_1, ...). In arguments these are unresolved
# references, not data — they would be sent to the tool verbatim (cloud 302).
# Flagging the family rather than one literal keeps the guard from whack-a-mole
# churn as the model's improvisations vary. The canonical step:{id} form is
# excluded so the promote step's legitimate placeholder is never flagged.
_REFERENCE_MARKER_RE = re.compile(
    r"(?i)"
    r"(?:^|\W)\$[0-9]"  # $1, $1.product_id
    r"|(?:上一步|前一步|前序|前步|下一步|上一次|上面的结果)"
    r"|(?:prev(?:ious)?|last|from|refer(?:ence)?|source)"
    r"[ _-]*(?:step|output|result)"  # from_step_1, previous_output
    r"|step[ _:.-]*[0-9]"  # step1 / step:1 / step_1 / step 1
)


def _looks_like_reference(value: str) -> bool:
    if _CANONICAL_REF_RE.match(value):
        return False
    return _REFERENCE_MARKER_RE.search(value) is not None


class ToolSpec(BaseModel):
    """Minimal tool contract used by the validator.

    The app derives these from the tool registry (name + required parameter
    names); tests pass them directly.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    required_params: list[str] = Field(default_factory=list)


def _structural_errors(
    plan: Plan,
    tool_schemas: dict[str, ToolSpec],
    candidate_tools: list[str] | None,
) -> list[StateError]:
    errors: list[StateError] = []
    step_ids = {step.step_id for step in plan.steps}
    candidates = set(candidate_tools) if candidate_tools else None

    for step in plan.steps:
        spec = tool_schemas.get(step.tool_name)
        if spec is None:
            errors.append(
                StateError(
                    code="UNKNOWN_TOOL",
                    message=f"step {step.step_id} 引用不存在的工具 {step.tool_name}",
                    step_id=step.step_id,
                )
            )
        if candidates and step.tool_name not in candidates:
            errors.append(
                StateError(
                    code="OUT_OF_CANDIDATES",
                    message=f"step {step.step_id} 引用的工具 {step.tool_name} 不在意图过滤候选集中",
                    step_id=step.step_id,
                )
            )
        if spec is not None:
            for param in spec.required_params:
                if param not in step.arguments and param not in step.argument_sources:
                    errors.append(
                        StateError(
                            code="MISSING_REQUIRED_ARG",
                            message=f"step {step.step_id} 缺少必填参数 {param}",
                            step_id=step.step_id,
                        )
                    )
        for param, value in step.arguments.items():
            if isinstance(value, str) and _looks_like_reference(value):
                errors.append(
                    StateError(
                        code="UNRESOLVED_REFERENCE",
                        message=(
                            f"step {step.step_id} 参数 {param} 的值 {value!r} 是未识别的"
                            "跨步骤引用，必须用 argument_sources 的 step:{step_id} 形式"
                        ),
                        step_id=step.step_id,
                    )
                )
        for source in step.argument_sources.values():
            if not source.startswith(_STEP_SOURCE_PREFIX):
                continue
            ref = source.split(":", 1)[1]
            if ref not in step.depends_on:
                errors.append(
                    StateError(
                        code="BROKEN_ARGUMENT_SOURCE",
                        message=(
                            f"step {step.step_id} 的 argument_sources 引用 {ref}，"
                            "但该 step 不在 depends_on 中"
                        ),
                        step_id=step.step_id,
                    )
                )
            if ref not in step_ids:
                errors.append(
                    StateError(
                        code="BROKEN_ARGUMENT_SOURCE",
                        message=f"step {step.step_id} 的 argument_sources 引用不存在的 step {ref}",
                        step_id=step.step_id,
                    )
                )
        for dep in step.depends_on:
            if dep not in step_ids:
                errors.append(
                    StateError(
                        code="MISSING_DEPENDENCY",
                        message=f"step {step.step_id} 依赖不存在的 step {dep}",
                        step_id=step.step_id,
                    )
                )
        if step.risk_level in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS) and not step.fallback:
            errors.append(
                StateError(
                    code="WRITE_NEEDS_FALLBACK",
                    message=f"step {step.step_id} 为写操作但缺少补偿/回退说明",
                    step_id=step.step_id,
                )
            )
        if step.tool_name in WRITE_TOOLS and step.risk_level == ToolRiskLevel.READ:
            errors.append(
                StateError(
                    code="RISK_DOWNGRADE",
                    message=(
                        f"step {step.step_id} 将已知写工具 {step.tool_name} 标记为 READ，"
                        "写操作必须按 WRITE 走审批闸门"
                    ),
                    step_id=step.step_id,
                )
            )
    return errors


def _build_graph(plan: Plan) -> tuple[dict[str, int], dict[str, list[str]]]:
    indeg: dict[str, int] = {step.step_id: 0 for step in plan.steps}
    children: dict[str, list[str]] = {step.step_id: [] for step in plan.steps}
    ids = set(indeg)
    for step in plan.steps:
        for dep in step.depends_on:
            if dep in ids:
                children[dep].append(step.step_id)
                indeg[step.step_id] += 1
    return indeg, children


def _topological_order(plan: Plan) -> tuple[list[str], bool]:
    """Kahn's algorithm; returns (order, has_cycle). Deterministic (sorted)."""
    indeg, children = _build_graph(plan)
    order: list[str] = []
    remaining = set(indeg)
    while remaining:
        ready = sorted(sid for sid in remaining if indeg[sid] == 0)
        if not ready:
            return [], True
        for sid in ready:
            order.append(sid)
            remaining.discard(sid)
            for child in children[sid]:
                indeg[child] -= 1
    return order, False


def _parallel_groups(plan: Plan) -> list[list[str]]:
    """Wave partition for the executor: READ steps share a group, WRITE steps
    are serial singletons. Only meaningful when the plan is acyclic."""
    indeg, children = _build_graph(plan)
    risk = {step.step_id: step.risk_level for step in plan.steps}
    groups: list[list[str]] = []
    remaining = set(indeg)
    while remaining:
        ready = sorted(sid for sid in remaining if indeg[sid] == 0)
        if not ready:
            return []
        writes = [sid for sid in ready if risk[sid] != ToolRiskLevel.READ]
        reads = [sid for sid in ready if risk[sid] == ToolRiskLevel.READ]
        for sid in writes:
            groups.append([sid])
        if reads:
            groups.append(reads)
        for sid in ready:
            remaining.discard(sid)
            for child in children[sid]:
                indeg[child] -= 1
    return groups


def validate_plan(
    plan: Plan,
    tool_schemas: dict[str, ToolSpec],
    candidate_tools: list[str] | None = None,
) -> PlanValidation:
    """Validate the plan DAG and compute scheduling hints.

    *candidate_tools* is the intent-filtered set from build_plan; when empty it
    is treated as unconstrained (the smoke/no-op graph has no candidates).
    """
    errors = _structural_errors(plan, tool_schemas, candidate_tools)
    order, has_cycle = _topological_order(plan)
    if has_cycle:
        errors.append(StateError(code="PLAN_CYCLE", message="计划存在循环依赖"))
    return PlanValidation(
        is_valid=not errors,
        errors=errors,
        topological_order=order,
        parallel_groups=_parallel_groups(plan) if not has_cycle else [],
    )


def build_validate_plan_node(
    *,
    tool_schemas: dict[str, ToolSpec],
) -> Callable[[AgentState], dict[str, Any]]:
    """Build the validate_plan LangGraph node with injected tool schemas.

    Writes the PlanValidation into state.plan_validation and merges validation
    errors into state.errors so the graph routes to recovery on an invalid plan.
    """

    def validate_plan_node(state: AgentState) -> dict[str, Any]:
        if state.plan is None:
            error = StateError(code="NO_PLAN", message="validate_plan 运行时没有 plan")
            return {
                "plan_validation": PlanValidation(is_valid=False, errors=[error]),
                "errors": [*state.errors, error],
            }
        validation = validate_plan(state.plan, tool_schemas, state.candidate_tools)
        updates: dict[str, Any] = {"plan_validation": validation}
        if validation.errors:
            updates["errors"] = [*state.errors, *validation.errors]
        return updates

    return validate_plan_node
