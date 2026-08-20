"""Worker LLM planner wiring — real Tier2/Tier3 client for the three-layer funnel.

``build_real_llm_complete`` is the single OpenAI-compatible chat factory (moved
from the eval so eval and worker share the A/B-proven client configuration);
``resolve_llm_plan_node`` wraps it in ``llm_call`` observability and builds the
Tier2/Tier3 ``build_plan_node``. The module stays Celery-free so the API
approve-resume path can import it without bootstrapping the broker
(graph_builder keeps its no-celery guarantee).

The LLM funnel is on by default: ``llm_planning_enabled`` (default true) makes
tier2/3 out-of-vocabulary queries go through LLM planning. ``resolve_llm_plan_node``
is three-state — disabled returns ``None`` (worker offline, tier2/3 honest-fail
``ROUTED_TIER23_NO_LLM``), enabled-without-key returns a node that fails with
``LLM_NOT_CONFIGURED`` (a configuration error, not a silent offline fallback),
enabled-with-key returns the real node.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai import OpenAI
from openai.types import CompletionUsage

from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS
from erp_copilot.agent.nodes.build_plan import build_plan_node
from erp_copilot.agent.state import AgentState, StateError
from erp_copilot.infrastructure.config import Settings
from erp_copilot.observability.langfuse import llm_call


def build_real_llm_complete(settings: Settings) -> Callable[[str], str]:
    """OpenAI-compatible chat client (DeepSeek) backed by Settings."""
    client = _build_client(settings)

    def llm_complete(prompt: str) -> str:
        return _chat_once(client, settings.llm_model, prompt)[0]

    return llm_complete


def _llm_not_configured_node() -> Callable[[AgentState], dict[str, Any]]:
    """Build the tier2/3 node for enabled-but-missing-key deployments.

    The LLM funnel is on by default, so a key-less deployment that routes an
    out-of-vocabulary query to tier2/3 must fail with an explicit configuration
    error (``LLM_NOT_CONFIGURED``) rather than silently falling offline — the
    deterministic layer must never over-grab a query the funnel decided to
    escalate.
    """

    def node(state: AgentState) -> dict[str, Any]:
        return {
            "plan": None,
            "errors": [
                *state.errors,
                StateError(
                    code="LLM_NOT_CONFIGURED",
                    message=(
                        "LLM 规划已启用（llm_planning_enabled=true）但未配置 "
                        "LLM API key，无法服务 Tier2/Tier3 词表外查询"
                    ),
                ),
            ],
        }

    return node


def resolve_llm_plan_node(
    settings: Settings,
) -> Callable[[AgentState], dict[str, Any]] | None:
    """Build the Tier2/Tier3 LLM plan node, or None when the funnel is disabled.

    Three states: ``llm_planning_enabled=false`` returns None so the graph's
    tier2/3 node honest-fails (``ROUTED_TIER23_NO_LLM``) and the worker stays
    offline; enabled without an API key returns a node that fails with
    ``LLM_NOT_CONFIGURED`` (a deployment configuration error, surfaced when an
    out-of-vocabulary query arrives); enabled with a key returns the real
    ``build_plan_node``, with the chat call wrapped in ``llm_call`` so each
    planning call lands on an OTel span, an ``LLM_CALL`` log line and a
    Langfuse generation.
    """
    if not settings.llm_planning_enabled:
        return None
    if not settings.llm_api_key.get_secret_value():
        return _llm_not_configured_node()
    client = _build_client(settings)
    model = settings.llm_model

    def observed_llm_complete(prompt: str) -> str:
        with llm_call(model=model, prompt=prompt) as call:
            content, usage = _chat_once(client, model, prompt)
            call.completion = content
            if usage is not None:
                call.input_tokens = usage.prompt_tokens
                call.output_tokens = usage.completion_tokens
        return call.completion or ""

    return build_plan_node(
        llm_complete=observed_llm_complete,
        available_tools=set(WORKER_TOOL_SCHEMAS),
        tool_schemas=WORKER_TOOL_SCHEMAS,
    )


def _build_client(settings: Settings) -> OpenAI:
    return OpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
    )


def _chat_once(
    client: OpenAI,
    model: str,
    prompt: str,
) -> tuple[str, CompletionUsage | None]:
    """One chat completion; returns (content, usage-or-None)."""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        # DeepSeek JSON mode: fixes the baseline's 2 unparseable outputs
        # (plan-047/048). The prompt already names JSON, which the mode
        # requires.
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "", resp.usage
