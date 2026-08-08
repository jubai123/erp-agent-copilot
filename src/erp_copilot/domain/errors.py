"""Unified error model for ERP Agent Copilot.

All copilot errors inherit from :class:`CopilotError`, so callers can
catch a single base class and still discriminate specific failures
via ``isinstance``.
"""

from __future__ import annotations


class CopilotError(Exception):
    """Base error for every copilot-originated exception.

    Attributes:
        message: Human-readable error description.
        code: Machine-readable error code (e.g. ``VALIDATION_ERROR``).
        details: Optional dict with field-level or context-specific info.
    """

    def __init__(self, message: str, *, code: str, details: dict | None = None) -> None:
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.code!r}, message={self.message!r})"


class ValidationError(CopilotError):
    """Input validation or schema mismatch."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "VALIDATION_ERROR",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class AuthorizationError(CopilotError):
    """Permission denied or insufficient scope."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "AUTHORIZATION_ERROR",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class NotFoundError(CopilotError):
    """A requested resource does not exist."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "NOT_FOUND",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class ToolExecutionError(CopilotError):
    """A tool failed during execution."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "TOOL_EXECUTION_ERROR",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class RetrievalError(CopilotError):
    """Knowledge / document retrieval failed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "RETRIEVAL_ERROR",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class AgentRuntimeError(CopilotError):
    """Agent planning, execution, or verification error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "AGENT_RUNTIME_ERROR",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class IdempotencyConflictError(CopilotError):
    """A write operation with the same idempotency key is already in flight."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "IDEMPOTENCY_CONFLICT",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class ConfigError(CopilotError):
    """Application configuration is invalid or missing."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "CONFIG_ERROR",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
