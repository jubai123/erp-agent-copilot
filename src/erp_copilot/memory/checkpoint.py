"""Checkpoint and recovery primitives — task 4.12.

Persists a full AgentState snapshot after every graph node, so a crashed
worker can resume from the latest checkpoint (docs/03 §7). Each save is
committed immediately — crash recovery only works if the write is durable
before the next node runs.

Resume is not LangGraph's built-in checkpointer (that would require the
langgraph-checkpoint-postgres package, an unapproved dependency); the worker
re-invokes the graph with the restored state. Re-running the side-effect-free
nodes (classify/retrieve/build_plan) is harmless, and execute_ready_steps
skips steps that already hold a COMPLETED result, so tool calls are never
re-executed.

run_status stores the persistence view (RunStatus) mapped from the runtime
AgentStatus — the two lifecycles are separate by design (state.py); the full
AgentStatus lives in state_json.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.orm import Session

from erp_copilot.agent.state import AgentState, AgentStatus
from erp_copilot.domain.entities import AgentCheckpoint
from erp_copilot.domain.enums import RunStatus

SyncNode = Callable[[AgentState], dict[str, Any]]
AsyncNode = Callable[[AgentState], Awaitable[dict[str, Any]]]
# Invoked after a checkpoint save with the node name and post-node state;
# the worker injects a sink that records lifecycle events (task 4.14).
EventSink = Callable[[str, AgentState], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def map_agent_status(status: AgentStatus) -> RunStatus:
    """Map the runtime lifecycle onto the persisted RunStatus.

    Runtime phases that are still working all map to RUNNING; EXPIRED is a
    terminal failure on the persistence side.
    """
    mapping: dict[AgentStatus, RunStatus] = {
        AgentStatus.QUEUED: RunStatus.PENDING,
        AgentStatus.PLANNING: RunStatus.PENDING,
        AgentStatus.WAITING_APPROVAL: RunStatus.WAITING_APPROVAL,
        AgentStatus.EXECUTING: RunStatus.RUNNING,
        AgentStatus.VERIFYING: RunStatus.RUNNING,
        AgentStatus.RETRYING: RunStatus.RUNNING,
        AgentStatus.REPLANNING: RunStatus.RUNNING,
        AgentStatus.SUCCEEDED: RunStatus.COMPLETED,
        AgentStatus.FAILED: RunStatus.FAILED,
        AgentStatus.EXPIRED: RunStatus.FAILED,
        AgentStatus.CANCELLED: RunStatus.CANCELLED,
    }
    return mapping[status]


class CheckpointSaver:
    """Persist and reload AgentState snapshots for one run.

    The session is injected (the app passes the Postgres session factory's
    session; unit tests pass an in-memory SQLite session). The clock is an
    injectable seam so tests can order saves deterministically.
    """

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] = _utcnow,
        event_sink: EventSink | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._event_sink = event_sink

    def save(self, node_name: str, state: AgentState) -> None:
        """Persist *state* as the checkpoint for *node_name*, committing now.

        When an *event_sink* is injected it runs before the commit, so the
        event it appends lands in the same transaction as the checkpoint.
        Default None keeps the saver a pure recovery primitive (task 4.12).
        """
        self._session.add(
            AgentCheckpoint(
                run_id=state.run_id,
                tenant_id=state.tenant_id,
                node_name=node_name,
                run_status=map_agent_status(state.status),
                state_json=state.model_dump_json(),
                created_at=self._clock(),
            )
        )
        if self._event_sink is not None:
            self._event_sink(node_name, state)
        self._session.commit()

    def load_latest(self, run_id: str, tenant_id: str) -> AgentState | None:
        """Return the most recent checkpoint for *run_id*, or None.

        The tenant filter keeps recovery inside the tenant's data boundary —
        a worker must never resume another tenant's run.
        """
        row = (
            self._session.query(AgentCheckpoint)
            .filter(
                AgentCheckpoint.run_id == run_id,
                AgentCheckpoint.tenant_id == tenant_id,
            )
            .order_by(AgentCheckpoint.created_at.desc(), AgentCheckpoint.id.desc())
            .first()
        )
        if row is None:
            return None
        return AgentState.model_validate_json(row.state_json)


def checkpointed(
    node_name: str,
    node: SyncNode | AsyncNode,
    saver: CheckpointSaver,
) -> SyncNode | AsyncNode:
    """Wrap one graph node so it persists the post-node state.

    The merged state mirrors LangGraph's per-field replacement: the node's
    update dict is applied over the input state. Preserves async-ness for the
    async execute_ready_steps node.
    """
    if inspect.iscoroutinefunction(node):

        async def _async_wrapped(state: AgentState) -> dict[str, Any]:
            updates = await cast(AsyncNode, node)(state)
            saver.save(node_name, state.model_copy(update=updates))
            return updates

        return _async_wrapped

    def _sync_wrapped(state: AgentState) -> dict[str, Any]:
        updates = cast(SyncNode, node)(state)
        saver.save(node_name, state.model_copy(update=updates))
        return updates

    return _sync_wrapped
