"""Append-only run event log primitives — task 4.14.

RunEvent is the immutable, append-only audit log for a run (docs/03 §3,
docs/07 run_events). Every append computes the next sequence so the SSE
stream (GET /v1/runs/{id}/events) can replay events in order and resume from
a client's Last-Event-ID. The session is injected (same style as
CheckpointSaver / FailureQueue); the caller commits, keeping the event
atomic with whatever it records.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from erp_copilot.domain.entities import RunEvent


def append_run_event(
    session: Session,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Append one RunEvent to *run_id*'s log, computing its sequence.

    The row is added to *session* but not committed — the caller commits so
    the event lands atomically with the state change it records.
    """
    max_seq = session.query(func.max(RunEvent.sequence)).filter(RunEvent.run_id == run_id).scalar()
    session.add(
        RunEvent(
            run_id=run_id,
            sequence=int(max_seq) + 1 if max_seq is not None else 0,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False),
        )
    )
