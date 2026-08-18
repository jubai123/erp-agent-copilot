"""Worker LLM planner wiring — real Tier2/Tier3 client for the three-layer funnel.

``build_real_llm_complete`` is the single OpenAI-compatible chat factory (moved
from the eval so eval and worker share the A/B-proven client configuration);
``resolve_llm_plan_node`` wraps it in ``llm_call`` observability and builds the
Tier2/Tier3 ``build_plan_node``. The module stays Celery-free so the API
approve-resume path can import it without bootstrapping the broker
(graph_builder keeps its no-celery guarantee).

The worker stays offline by default: an empty ``llm_api_key`` makes
``resolve_llm_plan_node`` return ``None``, so tier2/3 queries keep failing
honestly (``ROUTED_TIER23_NO_LLM``) instead of being over-grabbed by the
deterministic layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai import OpenAI
from openai.types import CompletionUsage

from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS
from erp_copilot.agent.nodes.build_plan import build_plan_node
from erp_copilot.agent.state import AgentState
from erp_copilot.infrastructure.config import Settings
from erp_copilot.observability.langfuse import llm_call


def build_real_llm_complete(settings: Settings) -> Callable[[str], str]:
    """OpenAI-compatible chat client (DeepSeek) backed by Settings."""
    client = _build_client(settings)

    def llm_complete(prompt: str) -> str:
        return _chat_once(client, settings.llm_model, prompt)[0]

    return llm_complete


def resolve_llm_plan_node(
    settings: Settings,
) -> Callable[[AgentState], dict[str, Any]] | None:
    """Build the Tier2/Tier3 LLM plan node; None when no LLM is configured.

    Mirrors ``resolve_erp_executor``'s configured-or-offline fallback: an empty
    ``llm_api_key`` returns None so the graph's tier2/3 node honest-fails
    (``ROUTED_TIER23_NO_LLM``) and the worker stays offline. When configured,
    the chat call is wrapped in ``llm_call`` so each planning call lands on an
    OTel span, an ``LLM_CALL`` log line and a Langfuse generation.
    """
    if not settings.llm_api_key.get_secret_value():
        return None
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
