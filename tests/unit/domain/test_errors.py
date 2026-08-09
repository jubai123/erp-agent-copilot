"""Tests for domain error model."""

import pytest

from erp_copilot.domain.errors import (
    AgentRuntimeError,
    AuthorizationError,
    ConfigError,
    CopilotError,
    NotFoundError,
    RetrievalError,
    ToolExecutionError,
    ValidationError,
)


class TestCopilotError:
    def test_base_error_stores_message_code_details(self) -> None:
        error = CopilotError("something went wrong", code="ERR_GENERIC")
        assert error.message == "something went wrong"
        assert error.code == "ERR_GENERIC"
        assert error.details == {}

    def test_base_error_stores_optional_details(self) -> None:
        error = CopilotError("invalid input", code="ERR_INPUT", details={"field": "name"})
        assert error.details == {"field": "name"}

    def test_str_returns_message(self) -> None:
        error = CopilotError("test message", code="ERR_TEST")
        assert str(error) == "test message"

    def test_repr_includes_code_and_message(self) -> None:
        error = CopilotError("boom", code="ERR_BOOM")
        assert "CopilotError" in repr(error)
        assert "ERR_BOOM" in repr(error)

    def test_subclasses_inherit_from_copilot_error(self) -> None:
        assert issubclass(ValidationError, CopilotError)
        assert issubclass(AuthorizationError, CopilotError)
        assert issubclass(NotFoundError, CopilotError)
        assert issubclass(ToolExecutionError, CopilotError)
        assert issubclass(RetrievalError, CopilotError)
        assert issubclass(AgentRuntimeError, CopilotError)
        assert issubclass(ConfigError, CopilotError)


class TestErrorDiscrimination:
    """isinstance must reliably distinguish every error type."""

    def test_validation_vs_authorization(self) -> None:
        val = ValidationError("bad input", code="ERR_VAL")
        auth = AuthorizationError("denied", code="ERR_AUTH")

        assert isinstance(val, ValidationError)
        assert not isinstance(val, AuthorizationError)
        assert isinstance(auth, AuthorizationError)
        assert not isinstance(auth, ValidationError)

    def test_catch_by_base_class(self) -> None:
        try:
            raise ToolExecutionError("tool crashed", code="ERR_TOOL")
        except CopilotError as e:
            assert e.code == "ERR_TOOL"
            assert isinstance(e, ToolExecutionError)

    def test_each_subclass_has_default_code(self) -> None:
        assert ValidationError("x").code == "VALIDATION_ERROR"
        assert AuthorizationError("x").code == "AUTHORIZATION_ERROR"
        assert NotFoundError("x").code == "NOT_FOUND"
        assert ToolExecutionError("x").code == "TOOL_EXECUTION_ERROR"
        assert RetrievalError("x").code == "RETRIEVAL_ERROR"
        assert AgentRuntimeError("x").code == "AGENT_RUNTIME_ERROR"
        assert ConfigError("x").code == "CONFIG_ERROR"

    def test_default_code_can_be_overridden(self) -> None:
        error = ValidationError("x", code="CUSTOM_VALIDATION")
        assert error.code == "CUSTOM_VALIDATION"


@pytest.mark.parametrize(
    "error_cls",
    [
        ValidationError,
        AuthorizationError,
        NotFoundError,
        ToolExecutionError,
        RetrievalError,
        AgentRuntimeError,
        ConfigError,
    ],
)
def test_all_subclasses_accept_message_only(error_cls: type[CopilotError]) -> None:
    error = error_cls("bare message")
    assert error.message == "bare message"
    assert isinstance(error, CopilotError)
