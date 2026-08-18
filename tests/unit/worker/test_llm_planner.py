"""Unit tests for the worker's real-LLM plan-node wiring (Tier2/Tier3 funnel).

``resolve_llm_plan_node`` is the worker's seam for the three-layer funnel's
Tier2/Tier3 planner: it builds the real ``build_plan_node`` backed by an
OpenAI-compatible chat client (DeepSeek) from Settings, wrapped in ``llm_call``
so every planning call lands on an OTel span, an ``LLM_CALL`` log line and a
Langfuse generation. Mirroring ``resolve_erp_executor``'s "configured or
offline fallback" pattern, an empty ``llm_api_key`` returns ``None`` so the
worker stays offline and tier2/3 queries keep failing honestly
(``ROUTED_TIER23_NO_LLM``) instead of over-grabbing.

The shared ``build_real_llm_complete`` factory (moved here from the eval so
eval and worker share the A/B-proven client configuration) is exercised with a
fake client, so no network is ever touched.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

import apps.worker.llm_planner as llm_planner
import erp_copilot.observability.tracing as tracing_mod
from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS
from apps.worker.llm_planner import build_real_llm_complete, resolve_llm_plan_node
from erp_copilot.infrastructure.config import Settings
from erp_copilot.observability.tracing import setup_tracing

_LOGGER_NAME = "erp_copilot.observability.langfuse"


@pytest.fixture(autouse=True)
def _reset_tracing() -> Iterator[None]:
    """Reset the module-scoped tracer so no exporter leaks across tests."""
    tracing_mod._provider = None
    yield
    tracing_mod._provider = None


def _settings_with_key() -> Settings:
    return Settings(llm_api_key=SecretStr("fake-key"))


class _FakeUsage:
    prompt_tokens = 5
    completion_tokens = 7


class _FakeMessage:
    content = "{}"


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]
    usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = _FakeChat()


def test_resolve_returns_none_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _should_not_build(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not build a plan node without an LLM key")

    monkeypatch.setattr(llm_planner, "build_plan_node", _should_not_build)

    assert resolve_llm_plan_node(Settings()) is None


def test_resolve_wires_worker_tool_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _capture_build(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return lambda state: {}

    monkeypatch.setattr(llm_planner, "build_plan_node", _capture_build)

    node = resolve_llm_plan_node(_settings_with_key())

    assert callable(node)
    assert captured["available_tools"] == set(WORKER_TOOL_SCHEMAS)
    assert captured["tool_schemas"] == WORKER_TOOL_SCHEMAS
    assert callable(captured["llm_complete"])


def test_resolve_llm_complete_calls_through_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _capture_build(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return lambda state: {}

    monkeypatch.setattr(llm_planner, "build_plan_node", _capture_build)
    monkeypatch.setattr(llm_planner, "_build_client", lambda _settings: _FakeClient())
    monkeypatch.setattr(llm_planner, "_chat_once", lambda _c, _m, p: ("{}", None))

    resolve_llm_plan_node(_settings_with_key())

    # The llm_call wrapper must pass the completion through untouched.
    assert captured["llm_complete"]("prompt-1") == "{}"


def test_build_real_llm_complete_uses_proven_client_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(llm_planner, "_build_client", lambda _settings: client)

    settings = _settings_with_key()
    llm_complete = build_real_llm_complete(settings)

    assert llm_complete("查苹果库存") == "{}"
    calls = client.chat.completions.calls
    assert len(calls) == 1
    assert calls[0]["model"] == settings.llm_model
    assert calls[0]["temperature"] == 0.2
    assert calls[0]["messages"] == [{"role": "user", "content": "查苹果库存"}]
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_resolve_llm_complete_backfills_tokens_onto_span_and_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Task 7.2: the production wiring's real call backfills token usage.

    The llm_call unit tests (test_langfuse.py) fill LlmCall fields by hand; this
    proves the *production* observed_llm_complete extracts usage from the chat
    response shape and lands input/output tokens + latency on the span and the
    LLM_CALL log — the D3 acceptance ("一次真实调用后 LLM_CALL 日志含
    model/input_tokens/output_tokens/latency_ms") rather than the context
    manager's mechanics.
    """
    exporter = InMemorySpanExporter()
    setup_tracing(service_name="svc", exporter=exporter)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    client = _FakeClient()
    monkeypatch.setattr(llm_planner, "_build_client", lambda _settings: client)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        llm_planner,
        "build_plan_node",
        lambda **kw: captured.update(kw) or (lambda state: {}),
    )

    resolve_llm_plan_node(_settings_with_key())
    result = captured["llm_complete"]("查苹果库存")
    model = client.chat.completions.calls[0]["model"]

    assert result == "{}"
    attrs = exporter.get_finished_spans()[0].attributes
    assert exporter.get_finished_spans()[0].name == f"llm.{model}"
    assert attrs["model"] == model
    assert attrs["input_tokens"] == 5
    assert attrs["output_tokens"] == 7
    assert attrs["latency_ms"] >= 0

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].event_type == "LLM_CALL"
    assert records[0].model == model
    assert records[0].input_tokens == 5
    assert records[0].output_tokens == 7
