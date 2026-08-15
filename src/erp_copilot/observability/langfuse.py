"""LLM call observability — task 6.3 (Langfuse SDK telemetry).

Every LLM invocation is recorded — prompt/response size, input/output token
counts, latency, and an estimated cost — onto an OTel span (task 6.2), a
structured log line (task 6.1), and a Langfuse ``generation`` observation, all
tagged with run_id so a run's LLM call chain is filterable in one query
(docs/08 §4).

The official :mod:`langfuse` SDK is wired in: :func:`configure_langfuse` reads
``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST`` and builds
the client; :func:`llm_call` records a generation observation when a client
exists. Observability stays fail-open — no keys means no client, and a Langfuse
hiccup never breaks the LLM call itself. Cost on the OTel span is estimated from
a small, clearly-marked price table — keep it current with the provider's
pricing.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langfuse import Langfuse
from opentelemetry import trace

from erp_copilot.observability.tracing import span

logger = logging.getLogger("erp_copilot.observability.langfuse")

# Configured once from env and cached; None when no keys are present (fail-open).
_client: Langfuse | None = None
_client_configured = False

# Estimated USD per 1K tokens, as (input, output). Illustrative prices for the
# models the project defaults to; update from the provider's pricing pages.
_PRICING_USD_PER_1K: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "deepseek-chat": (0.14, 0.28),
}


@dataclass
class LlmCall:
    """Mutable telemetry for one LLM invocation; the caller fills the fields."""

    model: str
    prompt: str
    system_prompt: str | None = None
    completion: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    error: str | None = None


def estimate_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """Estimate the USD cost of one call from token counts.

    Returns None when the model is not in the price table or token counts are
    missing — the caller then simply omits the cost attribute.
    """
    if input_tokens is None or output_tokens is None:
        return None
    price = _PRICING_USD_PER_1K.get(model)
    if price is None:
        return None
    in_price, out_price = price
    cost = (input_tokens / 1000.0) * in_price + (output_tokens / 1000.0) * out_price
    return round(cost, 6)


def configure_langfuse() -> Langfuse | None:
    """Build the Langfuse client from env vars; None when unconfigured.

    Runs once per process — later calls return the cached client. Missing keys
    cache a None so the env is not re-probed on every LLM call.
    """
    global _client, _client_configured
    if _client_configured:
        return _client
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    _client_configured = True
    if not public_key or not secret_key:
        _client = None
        return None
    _client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=os.getenv("LANGFUSE_HOST") or None,
    )
    return _client


def _get_client() -> Langfuse | None:
    if not _client_configured:
        configure_langfuse()
    return _client


@contextmanager
def llm_call(
    *,
    model: str,
    prompt: str,
    system_prompt: str | None = None,
) -> Iterator[LlmCall]:
    """Measure one LLM call, tagging a span, a log line, and a Langfuse generation.

    The caller runs the actual LLM request inside the block and sets
    ``completion`` / ``input_tokens`` / ``output_tokens`` on the yielded
    :class:`LlmCall`; on exit the span attributes, the ``LLM_CALL`` log, and the
    Langfuse observation are finalized. Exceptions are recorded as an ``error``
    attribute, the span is marked ERROR, the generation is marked ERROR, and the
    exception re-raised.
    """
    call = LlmCall(model=model, prompt=prompt, system_prompt=system_prompt)
    started = time.perf_counter()
    with span(f"llm.{model}") as active:
        obs = _start_observation(call)
        try:
            yield call
            call.latency_ms = _elapsed_ms(started)
            _finalize(call, active)
            _finish_observation(obs, call, error=None)
        except Exception as exc:
            call.error = str(exc)
            call.latency_ms = _elapsed_ms(started)
            _finalize(call, active)
            _finish_observation(obs, call, error=exc)
            raise


def _start_observation(call: LlmCall) -> Any | None:
    client = _get_client()
    if client is None:
        return None
    try:
        return client.start_observation(
            name=f"llm.{call.model}",
            as_type="generation",
            model=call.model,
            input=call.prompt,
        )
    except Exception:
        logger.warning("langfuse: start_observation failed", exc_info=True)
        return None


def _finish_observation(
    obs: Any | None,
    call: LlmCall,
    *,
    error: BaseException | None,
) -> None:
    if obs is None:
        return
    try:
        if error is not None:
            obs.update(level="ERROR", status_message=str(error))
        else:
            kwargs: dict[str, Any] = {}
            if call.completion is not None:
                kwargs["output"] = call.completion
            if call.input_tokens is not None and call.output_tokens is not None:
                kwargs["usage_details"] = {
                    "input": call.input_tokens,
                    "output": call.output_tokens,
                }
            obs.update(**kwargs)
        obs.end()
    except Exception:
        logger.warning("langfuse: observation update/end failed", exc_info=True)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 2)


def _finalize(call: LlmCall, active: trace.Span) -> None:
    """Write the call's telemetry onto the open span and a structured log."""
    attrs: dict[str, Any] = {"model": call.model}
    if call.input_tokens is not None:
        attrs["input_tokens"] = call.input_tokens
    if call.output_tokens is not None:
        attrs["output_tokens"] = call.output_tokens
    if call.input_tokens is not None and call.output_tokens is not None:
        attrs["total_tokens"] = call.input_tokens + call.output_tokens
    if call.latency_ms is not None:
        attrs["latency_ms"] = call.latency_ms
    cost = estimate_cost(call.model, call.input_tokens, call.output_tokens)
    if cost is not None:
        attrs["estimated_cost_usd"] = cost
    if call.error is not None:
        attrs["error"] = call.error
    for key, value in attrs.items():
        active.set_attribute(key, value)
    logger.info(
        "LLM call",
        extra={
            "event_type": "LLM_CALL",
            "model": call.model,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "latency_ms": call.latency_ms,
            "error": call.error,
        },
    )
