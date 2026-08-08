"""Deterministic step executor node — tasks 4.9-4.10.

Defence layer 3: actually run the plan steps (docs/03 §5). Walks
plan_validation.parallel_groups wave by wave — the READ steps in a wave run
concurrently via asyncio.gather, WRITE steps (serial singleton waves from
validate_plan) never overlap. Each step's arguments are resolved at runtime
before the wave launches (user_query → the run's query text; step:{id} → the
same-named output param of that step's result). Only steps whose policy
decision is ALLOW run; a step whose dependencies did not reach COMPLETED is
skipped; a failed step records FAILED and appends a StateError so the graph
routes to recovery. When parallel_groups is empty (defensive — a valid plan
always carries it) the node falls back to serial execution over
topological_order.

The executor is an async (tool_name, arguments) → Awaitable[ToolResult]
callable — the real MCP Gateway is async and is injected directly, no sync
adapter. Retry/idempotency semantics (Phase 5) compose around the executor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    PlanStep,
    PolicyDecision,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus
from erp_copilot.tools.tool_result import ToolResult

_USER_SOURCE = "user_query"
_STEP_SOURCE_PREFIX = "step:"

# The app injects the async MCP Gateway call directly; tests substitute a stub.
Executor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


def resolve_arguments(
    step: PlanStep,
    query: str,
    step_results: dict[str, StepResult],
) -> tuple[dict[str, Any], StateError | None]:
    """Resolve argument_sources into concrete values for *step*.

    Literal arguments pass through; each argument_sources entry is resolved at
    runtime — "user_query" injects *query* verbatim, "step:{id}" pulls the
    same-named key from that step's result data. An unresolvable source returns
    a StateError so the caller marks the step failed rather than running a tool
    with a placeholder value.
    """
    args = dict(step.arguments)
    for name, source in step.argument_sources.items():
        if source == _USER_SOURCE:
            args[name] = query
            continue
        if source.startswith(_STEP_SOURCE_PREFIX):
            ref = source.split(":", 1)[1]
            result = step_results.get(ref)
            if result is None or result.data is None or name not in result.data:
                return {}, StateError(
                    code="ARGUMENT_RESOLUTION_FAILED",
                    message=(
                        f"step {step.step_id} 的参数 {name} 依赖 {source} 的输出，但该输出不可用"
                    ),
                    step_id=step.step_id,
                )
            args[name] = result.data[name]
            continue
        return {}, StateError(
            code="ARGUMENT_RESOLUTION_FAILED",
            message=f"step {step.step_id} 的参数 {name} 使用了未知来源 {source}",
            step_id=step.step_id,
        )
    return args, None


def _dependencies_completed(step: PlanStep, step_results: dict[str, StepResult]) -> bool:
    return all(
        step_results.get(dep) is not None and step_results[dep].status == StepStatus.COMPLETED
        for dep in step.depends_on
    )


def _to_step_result(step: PlanStep, result: ToolResult) -> StepResult:
    error = result.error
    if result.status == "SUCCEEDED":
        return StepResult(
            step_id=step.step_id,
            status=StepStatus.COMPLETED,
            data=result.data,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )
    return StepResult(
        step_id=step.step_id,
        status=StepStatus.FAILED,
        error_code=error.error_code if error else "EXECUTION_FAILED",
        error_message=error.error_message if error else "工具执行失败",
        is_retryable=error.is_retryable if error else False,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


def _skipped(step_id: str) -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.SKIPPED)


async def _run_step(
    step: PlanStep,
    arguments: dict[str, Any],
    executor: Executor,
) -> tuple[PlanStep, ToolResult]:
    """Await one tool call; a raised executor exception becomes a failed
    ToolResult so one bad step cannot kill its wave's gather."""
    try:
        return step, await executor(step.tool_name, arguments)
    except Exception as exc:
        return step, ToolResult.failure(
            tool_version_id=step.tool_name,
            error_code="EXECUTION_FAILED",
            error_message=str(exc),
        )


def build_execute_steps_node(
    *,
    executor: Executor,
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    """Build the execute_ready_steps LangGraph node with an injected executor."""

    async def execute_ready_steps_node(state: AgentState) -> dict[str, Any]:
        if state.plan is None or state.plan_validation is None:
            error = StateError(
                code="NO_PLAN",
                message="execute_ready_steps 运行时没有可执行的 plan",
            )
            return {"errors": [*state.errors, error], "status": AgentStatus.FAILED}

        validation = state.plan_validation
        groups = validation.parallel_groups or [
            [step_id] for step_id in validation.topological_order
        ]
        steps = {step.step_id: step for step in state.plan.steps}
        results = dict(state.step_results)
        errors: list[StateError] = []

        for group in groups:
            runnable: list[PlanStep] = []
            for step_id in group:
                step = steps[step_id]
                if state.policy_decisions.get(step_id) != PolicyDecision.ALLOW:
                    results[step_id] = _skipped(step_id)
                    continue
                if not _dependencies_completed(step, results):
                    results[step_id] = _skipped(step_id)
                    continue
                runnable.append(step)

            prepared: list[tuple[PlanStep, dict[str, Any]]] = []
            for step in runnable:
                arguments, resolve_error = resolve_arguments(step, state.query, results)
                if resolve_error is not None:
                    results[step.step_id] = StepResult(
                        step_id=step.step_id,
                        status=StepStatus.FAILED,
                        error_code=resolve_error.code,
                        error_message=resolve_error.message,
                    )
                    errors.append(resolve_error)
                    continue
                prepared.append((step, arguments))
            if not prepared:
                continue

            outcomes = await asyncio.gather(
                *(_run_step(step, arguments, executor) for step, arguments in prepared)
            )
            for step, result in outcomes:
                results[step.step_id] = _to_step_result(step, result)
                if result.status != "SUCCEEDED":
                    tool_error = result.error
                    errors.append(
                        StateError(
                            code=tool_error.error_code if tool_error else "EXECUTION_FAILED",
                            message=tool_error.error_message if tool_error else "工具执行失败",
                            step_id=step.step_id,
                        )
                    )

        updates: dict[str, Any] = {
            "step_results": results,
            "status": AgentStatus.EXECUTING,
        }
        if errors:
            updates["errors"] = [*state.errors, *errors]
        return updates

    return execute_ready_steps_node
