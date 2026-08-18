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
adapter. Idempotency (task 5.6) and per-step backoff retry (task 5.7) compose
around the executor: WRITE steps run at-most-once through an injected
IdempotencyStore, and transient failures are retried within the step's
max_retries budget via an injected AsyncRetryExecutor.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from erp_copilot.agent.retry_policy import AsyncRetryExecutor
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    PlanStep,
    PolicyDecision,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus
from erp_copilot.domain.errors import IdempotencyConflictError
from erp_copilot.tools.idempotency import IdempotencyStore
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


_EXTERNAL_OPERATION_FIELDS = {"createOrder": "order_id"}


def _external_operation_id(step: PlanStep, result: ToolResult) -> str | None:
    """Extract the external operation id from a successful write result.

    createOrder's returned order_id is the outside-world handle for the write;
    recovery later re-reads it (getOrderByOrderId) to confirm the operation took
    effect instead of blindly retrying (docs/06 §7 rule 3). Only a string handle
    on a succeeded result is captured — a non-dict result or a missing field
    keeps the ledger column NULL.
    """
    field = _EXTERNAL_OPERATION_FIELDS.get(step.tool_name)
    if field is None or result.status != "SUCCEEDED":
        return None
    data = result.data
    if not isinstance(data, dict):
        return None
    value = data.get(field)
    return value if isinstance(value, str) else None


def _make_call(
    step: PlanStep,
    arguments: dict[str, Any],
    executor: Executor,
) -> Callable[[], Awaitable[ToolResult]]:
    """Wrap one executor invocation as a callable for the retry executor.

    A raised executor exception becomes a failed (non-retryable) ToolResult, so
    one bad step cannot kill its wave's gather or loop forever on a retry.
    """

    async def call() -> ToolResult:
        try:
            return await executor(step.tool_name, arguments)
        except Exception as exc:
            return ToolResult.failure(
                tool_version_id=step.tool_name,
                error_code="EXECUTION_FAILED",
                error_message=str(exc),
            )

    return call


async def _run_step(
    step: PlanStep,
    arguments: dict[str, Any],
    executor: Executor,
    retry_executor: AsyncRetryExecutor | None,
) -> tuple[PlanStep, ToolResult]:
    """Run a READ step, retrying transient failures within the step budget.

    Without an injected *retry_executor* the step runs exactly once (the
    pre-retry-wiring behaviour).
    """
    call = _make_call(step, arguments, executor)
    if retry_executor is None:
        return step, await call()
    return step, await retry_executor.execute(call, max_retries=step.max_retries)


async def _run_step_idempotent(
    step: PlanStep,
    arguments: dict[str, Any],
    executor: Executor,
    store: IdempotencyStore,
    tenant_id: str,
    run_id: str,
    retry_executor: AsyncRetryExecutor | None,
) -> tuple[PlanStep, ToolResult]:
    """Run one WRITE step at-most-once through the idempotency store.

    Records the execution intent (PENDING) before the tool runs, persists the
    result on success, and replays a COMPLETED record's cached payload without
    re-invoking the executor (a retry or crash-resume maps to the same key). The
    store is synchronous, so the node drives begin/complete/fail around the
    awaited async tool call instead of store.execute().

    Transient failures are retried within the step's max_retries budget via the
    injected retry executor. The intent record stays PENDING across retries and
    is finalized only once — COMPLETED on success, FAILED when the budget is
    exhausted — so a crash mid-retry is reconciled by recovery (task 5.8)
    rather than blindly re-run.
    """
    key = step.idempotency_key
    assert key is not None  # dispatcher guarantees; guards mypy narrowing
    try:
        record = store.begin(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_payload=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            run_id=run_id,
            step_id=step.step_id,
        )
    except IdempotencyConflictError as exc:
        return step, ToolResult.failure(
            tool_version_id=step.tool_name,
            error_code="IDEMPOTENCY_CONFLICT",
            error_message=str(exc),
            is_retryable=True,
        )

    if record.status == "COMPLETED" and record.result_payload is not None:
        return step, ToolResult.success(
            tool_version_id=step.tool_name,
            data=json.loads(record.result_payload),
        )

    call = _make_call(step, arguments, executor)
    if retry_executor is None:
        result = await call()
    else:
        result = await retry_executor.execute(call, max_retries=step.max_retries)
    if result.status == "SUCCEEDED":
        store.complete(
            record,
            json.dumps(result.data or {}, ensure_ascii=False),
            external_operation_id=_external_operation_id(step, result),
        )
    else:
        error = result.error
        store.fail(record, error.error_message if error else "工具执行失败")
    return step, result


async def _run_one(
    step: PlanStep,
    arguments: dict[str, Any],
    executor: Executor,
    store: IdempotencyStore | None,
    tenant_id: str,
    run_id: str,
    retry_executor: AsyncRetryExecutor | None,
) -> tuple[PlanStep, ToolResult]:
    """Dispatch a step to the idempotent WRITE path or the plain runner.

    Only steps that carry an idempotency key (the planner stamps WRITE/DANGEROUS
    steps) go through the store; reads are naturally repeatable and stay on the
    fast path.
    """
    if store is not None and step.idempotency_key is not None:
        return await _run_step_idempotent(
            step, arguments, executor, store, tenant_id, run_id, retry_executor
        )
    return await _run_step(step, arguments, executor, retry_executor)


def build_execute_steps_node(
    *,
    executor: Executor,
    idempotency_store: IdempotencyStore | None = None,
    retry_executor: AsyncRetryExecutor | None = None,
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    """Build the execute_ready_steps LangGraph node with an injected executor.

    With an *idempotency_store* injected, WRITE steps execute at-most-once per
    key against the DB ledger (docs/06 §7); without one the node is DB-free and
    every step runs through the raw executor. With a *retry_executor* injected,
    transient failures are retried within each step's max_retries budget with
    exponential backoff; without one the executor runs each step exactly once.
    """

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
                prior = results.get(step_id)
                if prior is not None and prior.status == StepStatus.COMPLETED:
                    # Resume guard (task 4.12): a step completed in a previous
                    # graph pass must not be re-executed.
                    continue
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
                *(
                    _run_one(
                        step,
                        arguments,
                        executor,
                        idempotency_store,
                        state.tenant_id,
                        state.run_id,
                        retry_executor,
                    )
                    for step, arguments in prepared
                )
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
