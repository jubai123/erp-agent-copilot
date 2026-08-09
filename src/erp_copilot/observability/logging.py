"""Structured JSON logging and trace context — task 6.1.

Every log line is one JSON object so aggregators (Loki, CloudWatch, ELK) can
index fields instead of parsing free text (docs/08 §6). Run identity travels
in a :class:`contextvars.ContextVar` — any component in the call tree logs and
the ``run_id`` is attached automatically, which is what makes a single
``run_id`` searchable across API / worker / gateway logs. Pure stdlib: the
formatter is a :class:`logging.Formatter` subclass, so no third-party logging
dependency is needed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class TraceContext:
    """Identity fields attached to every log line within a scope."""

    run_id: str | None = None
    step_id: str | None = None
    request_id: str | None = None


# Default None (not a TraceContext instance) so the ContextVar holds no
# mutable data structure; get_trace_context() fills in the empty context.
_trace_context_var: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def get_trace_context() -> TraceContext:
    """Return the identity bound to the current execution context."""
    return _trace_context_var.get() or TraceContext()


@contextmanager
def trace_context(
    *,
    run_id: str | None = None,
    step_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """Bind identity fields for the block, merging with the outer context.

    Only the fields passed are overridden — an inner ``trace_context(step_id=...)``
    keeps the outer ``run_id``, so the agent can layer run → step contexts and
    every log line stays correlated to its run.
    """
    current = _trace_context_var.get() or TraceContext()
    merged = TraceContext(
        run_id=run_id if run_id is not None else current.run_id,
        step_id=step_id if step_id is not None else current.step_id,
        request_id=request_id if request_id is not None else current.request_id,
    )
    token = _trace_context_var.set(merged)
    try:
        yield
    finally:
        _trace_context_var.reset(token)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record with a stable field schema."""

    # LogRecord attributes the stdlib owns; everything else in record.__dict__
    # is caller-supplied and belongs in "extra".
    _STDLIB_ATTRS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
        }
    )

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_trace_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "run_id": ctx.run_id,
            "step_id": ctx.step_id,
            "request_id": ctx.request_id,
            "message": record.getMessage(),
            "extra": {
                key: value
                for key, value in record.__dict__.items()
                if key not in self._STDLIB_ATTRS
            },
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(*, level: str = "INFO", service: str = "erp-agent-copilot") -> None:
    """Point the root logger at a JSON StreamHandler (idempotent).

    Callers map :class:`~erp_copilot.infrastructure.config.Settings` onto
    ``level`` and ``service`` at the process entry point.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service=service))
    root.addHandler(handler)
    root.setLevel(level.upper())
