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

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session

from apps.worker.executor import erp_simulator_executor
from erp_copilot.agent.graph import build_agent_graph
from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node
from erp_copilot.agent.nodes.policy_check import build_policy_check_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.nodes.verify_results import build_verify_results_node
from erp_copilot.agent.planner import build_deterministic_plan_node
from erp_copilot.memory.checkpoint import CheckpointSaver
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

_WRITER_SCOPES: frozenset[str] = frozenset({"product:read", "supplier:read", "order:write"})


def resolve_worker_scopes(_tenant_id: str, _user_id: str) -> set[str]:
    """Scope resolver until RBAC lands.

    The worker executes as a system actor; granting order:write lets a WRITE
    plan reach the approval gate (REQUIRE_APPROVAL) instead of being DENY'd by
    the scope check — the human approval, not a missing scope, is what governs
    a write.
    """
    return set(_WRITER_SCOPES)


def build_worker_graph(
    session: Session,
    checkpoint_saver: CheckpointSaver,
) -> CompiledStateGraph:
    """Build the worker graph with real nodes, write scope and idempotent writes.

    *session* feeds the IdempotencyStore (the executor's DB ledger); the
    checkpoint saver is passed in so the caller wires its event sink (the
    worker) or not (the API resume path).
    """
    return build_agent_graph(
        plan_node=build_deterministic_plan_node(),
        validate_node=build_validate_plan_node(tool_schemas=WORKER_TOOL_SCHEMAS),
        policy_node=build_policy_check_node(get_scopes=resolve_worker_scopes),
        execute_node=build_execute_steps_node(
            executor=erp_simulator_executor,
            idempotency_store=IdempotencyStore(session),
        ),
        verify_node=build_verify_results_node(),
        checkpoint_saver=checkpoint_saver,
    )
