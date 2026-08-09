"""Unified ToolResult — one return shape across HTTP, gRPC, and MCP tools."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ARTIFACT_SIZE_THRESHOLD = 100_000  # bytes


@dataclass
class ToolError:
    """Structured error information embedded in a :class:`ToolResult`."""

    error_code: str
    error_message: str
    is_retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "error_message": self.error_message,
            "is_retryable": self.is_retryable,
        }


@dataclass
class ToolResult:
    """The universal shape for every tool execution outcome.

    Use the factory methods::

        ToolResult.success(tool_version_id="v1", data={...})
        ToolResult.failure(tool_version_id="v1", error_code="TIMEOUT", ...)
    """

    tool_version_id: str
    status: str  # SUCCEEDED or FAILED
    started_at: datetime
    finished_at: datetime
    attempt: int = 1
    data: dict[str, Any] | None = None
    artifact_uri: str | None = None
    error: ToolError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        tool_version_id: str,
        data: dict[str, Any],
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        attempt: int = 1,
        metadata: dict[str, Any] | None = None,
        artifact_dir: str | None = None,
    ) -> ToolResult:
        """Create a successful result, offloading large data to artifact."""
        now = datetime.now(UTC)
        started = started_at or now
        finished = finished_at or now

        result_data: dict[str, Any] | None = data
        artifact_uri: str | None = None

        if artifact_dir:
            serialized = json.dumps(data, ensure_ascii=False, default=str)
            if len(serialized.encode()) > _ARTIFACT_SIZE_THRESHOLD:
                artifact_path = Path(artifact_dir) / f"{uuid.uuid4().hex}.json"
                artifact_path.write_text(serialized, encoding="utf-8")
                artifact_uri = f"artifact://{artifact_path.name}"
                result_data = None

        return cls(
            tool_version_id=tool_version_id,
            status="SUCCEEDED",
            started_at=started,
            finished_at=finished,
            attempt=attempt,
            data=result_data,
            artifact_uri=artifact_uri,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        tool_version_id: str,
        error_code: str,
        error_message: str,
        *,
        is_retryable: bool = False,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        attempt: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Create a failed result with structured error information."""
        now = datetime.now(UTC)
        return cls(
            tool_version_id=tool_version_id,
            status="FAILED",
            started_at=started_at or now,
            finished_at=finished_at or now,
            attempt=attempt,
            error=ToolError(
                error_code=error_code,
                error_message=error_message,
                is_retryable=is_retryable,
            ),
            metadata=metadata or {},
        )

    @property
    def elapsed_ms(self) -> int:
        delta = self.finished_at - self.started_at
        return int(delta.total_seconds() * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool_version_id": self.tool_version_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "elapsed_ms": self.elapsed_ms,
            "attempt": self.attempt,
            "data": self.data,
            "artifact_uri": self.artifact_uri,
            "error": self.error.to_dict() if self.error else None,
            "metadata": self.metadata,
        }
