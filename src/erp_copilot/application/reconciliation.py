"""Terminal write-path reconciliation service — task 7.11.

After a run settles, verify that the ERP's actual final state matches the
plan's write intent and record the verdict on
``erp_reconciliation_success_total`` (outcome=consistent/mismatch). This is
the online sentinel for write-path correctness — "AI 说做了，系统真的做了吗" —
and upgrades the crash-recovery error code RECOVERY_RECONCILIATION_REQUIRED
(docs/03 §9) from an error into a production metric (docs/10 task 7.11).

Only COMPLETED WRITE/DANGEROUS steps are judged: a step that never ran (FAILED
/ SKIPPED) has no claimed effect to verify. The intent is the step's *resolved*
arguments — cross-step sources (step:s1) are resolved against the earlier
steps' results via the same pure resolve_arguments the executor uses, so a
chained createOrder DAG compares real values instead of the ``$1`` placeholders.
Steps that cannot be verified (no order handle, or the ERP read-back itself
failed) are skipped, not counted — the same None-skip discipline as
classify_answer_groundedness, so a transient ERP outage never fabricates a
mismatch.

The read-back of the ERP state is injected (``read_order``): tests drive a stub
and the offline worker stays network-free; build_erp_read_order adapts the
async tool executor (getOrderByOrderId) to a synchronous call.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from erp_copilot.agent.nodes.execute_steps import resolve_arguments
from erp_copilot.agent.state import AgentState
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from erp_copilot.observability.metrics import METRICS, Metrics
from erp_copilot.tools.tool_result import ToolResult

ReconciliationVerdict = Literal["consistent", "mismatch"]

# The order status a successful cancel must leave behind. The in-process
# simulator only creates orders today (status "CREATED"); the CANCELLED target
# is the contract for any ERP that implements cancelOrder.
_CANCELLED_STATUS = "CANCELLED"

# Write tools whose effect reconciliation can verify against a read-back order.
_VERIFIABLE_WRITE_TOOLS = frozenset({"createOrder", "updateOrderStatus", "cancelOrder"})

# Intent fields of createOrder that must survive on the created order.
_CREATE_INTENT_FIELDS = ("product_id", "quantity", "supplier_id", "region")


class ReconciliationQueryError(RuntimeError):
    """The ERP read-back itself failed (timeout, transport...); nothing proven."""


@dataclass(frozen=True)
class StepReconciliation:
    """Verdict for one write step: the ERP state satisfied the intent or not."""

    step_id: str
    tool_name: str
    verdict: ReconciliationVerdict
    detail: str


# Reads one order back from the ERP. None means the ERP holds no such order;
# a query that failed (timeout/transport) raises ReconciliationQueryError.
ReadOrder = Callable[[str], dict[str, Any] | None]


def verify_write_step(
    tool_name: str,
    arguments: dict[str, Any],
    actual: dict[str, Any] | None,
) -> ReconciliationVerdict | None:
    """Pure decision: does the ERP state *actual* satisfy the write intent?

    ``arguments`` must be the *resolved* step arguments (see resolve_arguments).
    None is returned for tools outside the verifiable write set, so a future
    write tool simply opts out until someone defines its consistency rule.
    """
    if tool_name == "createOrder":
        if actual is None:
            return "mismatch"
        for field in _CREATE_INTENT_FIELDS:
            if arguments.get(field) != actual.get(field):
                return "mismatch"
        return "consistent"
    if tool_name == "updateOrderStatus":
        if actual is None:
            return "mismatch"
        return "consistent" if actual.get("status") == arguments.get("status") else "mismatch"
    if tool_name == "cancelOrder":
        if actual is None:
            return "mismatch"
        return "consistent" if actual.get("status") == _CANCELLED_STATUS else "mismatch"
    return None


def _mismatch_detail(tool_name: str, arguments: dict[str, Any], actual: dict[str, Any]) -> str:
    if tool_name == "createOrder":
        for field in _CREATE_INTENT_FIELDS:
            if arguments.get(field) != actual.get(field):
                return (
                    f"createOrder 字段 {field} 不一致 (意图 {arguments.get(field)!r} "
                    f"vs ERP {actual.get(field)!r})"
                )
    return f"{tool_name}: ERP 状态与意图不一致"


def _write_handle(
    step_result: object, resolved_args: dict[str, Any]
) -> str | None:
    """The order handle to read back: the created order's id, else the intent's.

    createOrder returns the order (with its id) in the step result; cancel /
    update target an existing order whose id arrives via the resolved arguments
    (literal, or resolved from an earlier getOrderByOrderId step).
    """
    data = getattr(step_result, "data", None)
    if isinstance(data, dict):
        order_id = data.get("order_id")
        if isinstance(order_id, str):
            return order_id
    order_id = resolved_args.get("order_id")
    return order_id if isinstance(order_id, str) else None


def reconcile_write_paths(
    state: AgentState,
    read_order: ReadOrder,
) -> list[StepReconciliation]:
    """Verdicts for every executed write step; unverifiable steps are omitted.

    Never raises: a read-back failure skips that step, so a flaky ERP cannot
    take down persist_run or fabricate a mismatch.
    """
    if state.plan is None:
        return []
    verdicts: list[StepReconciliation] = []
    for step in state.plan.steps:
        if step.risk_level not in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
            continue
        step_result = state.step_results.get(step.step_id)
        if step_result is None or step_result.status != StepStatus.COMPLETED:
            continue
        resolved, resolve_error = resolve_arguments(step, state.query, state.step_results)
        if resolve_error is not None:
            continue  # intent not resolvable -> nothing to verify against.
        handle = _write_handle(step_result, resolved)
        if handle is None:
            continue
        try:
            actual = read_order(handle)
        except Exception:
            continue
        verdict = verify_write_step(step.tool_name, resolved, actual)
        if verdict is None:
            continue
        detail = (
            f"{step.tool_name}: ERP 状态与意图一致"
            if verdict == "consistent"
            else _mismatch_detail(step.tool_name, resolved, actual)
            if isinstance(actual, dict)
            else f"{step.tool_name}: ERP 中无对应订单"
        )
        verdicts.append(
            StepReconciliation(step_id=step.step_id, tool_name=step.tool_name,
                               verdict=verdict, detail=detail)
        )
    return verdicts


async def _await_it(coro: Awaitable[ToolResult]) -> ToolResult:
    return await coro


def _run_sync(coro: Awaitable[ToolResult]) -> ToolResult:
    """Run one async executor call to completion on the caller's thread.

    Mirrors recovery._run_sync: a running loop (the API approve path inside
    FastAPI) gets a dedicated thread with its own loop; a prefork worker has
    none and runs inline.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_it(coro))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_await_it(coro))).result()


def build_erp_read_order(
    executor: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
) -> ReadOrder:
    """Adapt the async tool executor's getOrderByOrderId to a sync read_order.

    ORDER_NOT_FOUND maps to None (the order genuinely does not exist); any other
    failure raises ReconciliationQueryError so the caller skips the step instead
    of reading a transient outage as "the order is missing".
    """

    def read_order(order_id: str) -> dict[str, Any] | None:
        result = _run_sync(executor("getOrderByOrderId", {"order_id": order_id}))
        if result.status == "SUCCEEDED" and isinstance(result.data, dict):
            return result.data
        error = result.error
        if error is not None and error.error_code == "ORDER_NOT_FOUND":
            return None
        raise ReconciliationQueryError(
            error.error_message if error is not None else "getOrderByOrderId failed"
        )

    return read_order


def build_terminal_reconciler(
    executor: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
    metrics: Metrics = METRICS,
) -> Callable[[AgentState], list[StepReconciliation]]:
    """Build the persist_run-facing reconciler: verdicts in, metric out.

    Wires the executor read-back and the erp_reconciliation_success_total
    counter (outcome=consistent/mismatch) around reconcile_write_paths. The
    worker and the API approve-resume path pass the same factory their resolved
    executor, so write correctness is measured on both terminal paths.
    """
    read_order = build_erp_read_order(executor)

    def reconciler(state: AgentState) -> list[StepReconciliation]:
        verdicts = reconcile_write_paths(state, read_order)
        for step_verdict in verdicts:
            metrics.reconciliation_success.labels(outcome=step_verdict.verdict).inc()
        return verdicts

    return reconciler
