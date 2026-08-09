"""Domain enumerations shared across all modules."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """Lifecycle status of an Agent run."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Status of a single step within a plan DAG."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolRiskLevel(StrEnum):
    """Risk classification for a tool operation.

    Ordered from least to most dangerous: READ < WRITE < DANGEROUS.
    """

    READ = "read"
    WRITE = "write"
    DANGEROUS = "dangerous"


class IngestionStatus(StrEnum):
    """Lifecycle status of a knowledge document ingestion task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
