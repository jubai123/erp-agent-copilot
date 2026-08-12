"""Worker graph factory — task 4.17 write-path enablement.

The worker's graph now grants the order:write scope, so a WRITE plan pauses at
request_approval (WAITING_APPROVAL) instead of being DENY-skipped as a silent
no-op, and wires an IdempotencyStore (docs/06 §7) into the execute node so
every WRITE step runs at most once per idempotency key. Everything the API
needs to resume an approved run (build_agent_graph with the same real nodes)
lives here.

The module must stay Celery-free: apps/api imports build_worker_graph for the
approve-run resume path, and importing apps.worker.celery_app at API module
load would bootstrap the broker/client eagerly.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from apps.worker.executor import erp_simulator_executor
from erp_copilot.agent.graph import build_agent_graph
from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node
from erp_copilot.agent.nodes.policy_check import build_policy_check_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.nodes.verify_results import build_verify_results_node
from erp_copilot.agent.planner import build_deterministic_plan_node
from erp_copilot.agent.state import AgentState
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.observability.metrics import METRICS
from erp_copilot.security.rbac import resolve_user_scopes
from erp_copilot.tools.idempotency import IdempotencyStore

# Tool schemas the deterministic planner can emit (docs/05 simulator tools).
WORKER_TOOL_SCHEMAS: dict[str, ToolSpec] = {
    "getProductByName": ToolSpec(name="getProductByName", required_params=["name"]),
    "getProductById": ToolSpec(name="getProductById", required_params=["product_id"]),
    "getProductSubstitutesByName": ToolSpec(
        name="getProductSubstitutesByName", required_params=["name"]
    ),
    "querySuppliersByDeliveryRegion": ToolSpec(
        name="querySuppliersByDeliveryRegion", required_params=["region"]
    ),
    "getSupplierByStatus": ToolSpec(name="getSupplierByStatus", required_params=["status"]),
    "getOrderByOrderId": ToolSpec(name="getOrderByOrderId", required_params=["order_id"]),
    "createOrder": ToolSpec(
        name="createOrder",
        required_params=["product_id", "supplier_id", "quantity", "region"],
    ),
    "updateOrderStatus": ToolSpec(name="updateOrderStatus", required_params=["order_id", "status"]),
    "cancelOrder": ToolSpec(name="cancelOrder", required_params=["order_id"]),
}

def _observe_phase(phase: str, node: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap one phase node so its wall-clock duration feeds the phase histogram.

    Mirrors CheckpointSaver.checkpointed's sync/async detection so the async
    execute node keeps its coroutine shape through the wrapper (LangGraph and
    the checkpoint wrapper branch on iscoroutinefunction). The measured span is
    the node body only — checkpoint persistence runs outside it.
    """
    if inspect.iscoroutinefunction(node):

        async def _async_wrapped(state: AgentState) -> dict[str, Any]:
            start = time.monotonic()
            updates = await node(state)
            METRICS.phase_latency.labels(phase=phase).observe(time.monotonic() - start)
            return updates

        return _async_wrapped

    def _sync_wrapped(state: AgentState) -> dict[str, Any]:
        start = time.monotonic()
        updates = node(state)
        METRICS.phase_latency.labels(phase=phase).observe(time.monotonic() - start)
        return updates

    return _sync_wrapped


def build_worker_graph(
    session: Session,
    checkpoint_saver: CheckpointSaver,
) -> CompiledStateGraph:
    """Build the worker graph with real nodes, write scope and idempotent writes.

    *session* feeds the IdempotencyStore (the executor's DB ledger); the
    checkpoint saver is passed in so the caller wires its event sink (the
    worker) or not (the API resume path). The plan/execute/verify phase nodes
    are wrapped by _observe_phase so the phase-latency histogram (task 6.4)
    feeds from both the worker and the API approve-resume path.
    """
    return build_agent_graph(
        plan_node=_observe_phase("plan", build_deterministic_plan_node()),
        validate_node=build_validate_plan_node(tool_schemas=WORKER_TOOL_SCHEMAS),
        policy_node=build_policy_check_node(get_scopes=partial(resolve_user_scopes, session)),
        execute_node=_observe_phase(
            "execute",
            build_execute_steps_node(
                executor=erp_simulator_executor,
                idempotency_store=IdempotencyStore(session),
            ),
        ),
        verify_node=_observe_phase("verify", build_verify_results_node()),
        checkpoint_saver=checkpoint_saver,
    )
