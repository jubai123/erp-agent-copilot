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
from erp_copilot.agent.nodes.retrieve_context import build_retrieve_context_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.nodes.verify_results import build_verify_results_node
from erp_copilot.agent.planner import build_deterministic_plan_node
from erp_copilot.agent.retry_policy import AsyncRetryExecutor
from erp_copilot.agent.state import AgentState, RetrievedDocument
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.observability.metrics import METRICS
from erp_copilot.retrieval.embedding import EmbeddingProvider
from erp_copilot.retrieval.fts import keyword_search as _keyword_search
from erp_copilot.retrieval.pipeline import DeterministicEmbeddingProvider
from erp_copilot.retrieval.vector_store import search_similar as _search_similar
from erp_copilot.security.injection_guard import (
    InjectionGuard,
    InjectionVerdict,
    record_injection_event,
)
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
            if not isinstance(updates, dict):
                raise TypeError(f"phase node returned {type(updates).__name__}, expected dict")
            return updates

        return _async_wrapped

    def _sync_wrapped(state: AgentState) -> dict[str, Any]:
        start = time.monotonic()
        updates = node(state)
        METRICS.phase_latency.labels(phase=phase).observe(time.monotonic() - start)
        if not isinstance(updates, dict):
            raise TypeError(f"phase node returned {type(updates).__name__}, expected dict")
        return updates

    return _sync_wrapped


def build_worker_retrieve_node(
    session: Session,
    *,
    run_id: str | None = None,
    injection_guard: InjectionGuard | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> Callable[[AgentState], dict[str, Any]] | None:
    """Bind the real retrieve_context node to *session*'s Postgres backends.

    Wires pgvector ``search_similar`` and Postgres FTS ``keyword_search`` into
    the L2 retrieve node and attaches the injection guard plus quarantine
    recorder, so the worker and approve-resume paths run real hybrid retrieval
    with knowledge-layer screening (task 5.4). The search SQL is Postgres-only
    (``<=>`` / ``ts_rank``), so a non-PostgreSQL session — the SQLite unit
    suite — returns ``None`` and the graph keeps its no-op retrieve node.
    Embedding defaults to the deterministic provider so the execution hot path
    stays offline and reproducible, mirroring the eval harness.
    """
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if dialect is None or getattr(dialect, "name", "") != "postgresql":
        return None

    provider = embedding_provider or DeterministicEmbeddingProvider()
    guard = injection_guard or InjectionGuard()

    def record_quarantine(doc: RetrievedDocument, verdict: InjectionVerdict) -> None:
        record_injection_event(
            session,
            run_id=run_id,
            text=doc.content,
            verdict=verdict,
            disposition="quarantined",
        )

    return build_retrieve_context_node(
        embed=lambda text: provider.embed([text])[0],
        vector_search=lambda embedding, top_k, tenant_id: _search_similar(
            session, embedding, tenant_id, top_k=top_k
        ),
        keyword_search=lambda query, top_k, tenant_id: _keyword_search(
            session, query, tenant_id, top_k=top_k
        ),
        injection_guard=guard,
        record_quarantine=record_quarantine,
    )


def build_worker_graph(
    session: Session,
    checkpoint_saver: CheckpointSaver,
    *,
    retrieve_node: Callable[[AgentState], dict[str, Any]] | None = None,
    run_id: str | None = None,
    executor: Callable[..., Any] | None = None,
    llm_plan_node: Callable[[AgentState], dict[str, Any]] | None = None,
) -> CompiledStateGraph:
    """Build the worker graph with real nodes, write scope and idempotent writes.

    *session* feeds the IdempotencyStore (the executor's DB ledger); the
    checkpoint saver is passed in so the caller wires its event sink (the
    worker) or not (the API resume path). The plan/execute/verify phase nodes
    are wrapped by _observe_phase so the phase-latency histogram (task 6.4)
    feeds from both the worker and the API approve-resume path.

    *retrieve_node* defaults to the session-bound real retrieval node
    (build_worker_retrieve_node) and falls back to the graph's no-op retrieve
    node on non-PostgreSQL sessions — keeping the SQLite unit suite on the
    no-op without an explicit flag. *run_id* threads the run into the
    quarantine recorder so flagged knowledge lands in security_events keyed to
    the run.

    *executor* resolves tool calls inside execute_ready_steps; it defaults to
    the in-process ERP simulator so offline/test builds make no network calls.
    Callers that want the cloud ERP pass resolve_erp_executor(settings) — see
    apps.worker.executor.

    *llm_plan_node* is the three-layer funnel's Tier2/Tier3 planner (e.g.
    build_plan_node bound to a real LLM client). Omitted by default so the
    worker stays offline: tier1 queries use the deterministic planner and
    tier2/3 queries fail honestly (ROUTED_TIER23_NO_LLM) instead of being
    over-grabbed by the deterministic layer. When injected, it is observed
    under the distinct "plan_llm" phase label so the latency histogram keeps
    LLM planning separate from deterministic planning.
    """
    if retrieve_node is None:
        retrieve_node = build_worker_retrieve_node(session, run_id=run_id)
    llm_node = None if llm_plan_node is None else _observe_phase("plan_llm", llm_plan_node)
    return build_agent_graph(
        retrieve_node=retrieve_node,
        plan_node=_observe_phase("plan", build_deterministic_plan_node()),
        llm_plan_node=llm_node,
        validate_node=build_validate_plan_node(tool_schemas=WORKER_TOOL_SCHEMAS),
        policy_node=build_policy_check_node(get_scopes=partial(resolve_user_scopes, session)),
        execute_node=_observe_phase(
            "execute",
            build_execute_steps_node(
                executor=executor or erp_simulator_executor,
                idempotency_store=IdempotencyStore(session),
                retry_executor=AsyncRetryExecutor(),
            ),
        ),
        verify_node=_observe_phase("verify", build_verify_results_node()),
        checkpoint_saver=checkpoint_saver,
    )
