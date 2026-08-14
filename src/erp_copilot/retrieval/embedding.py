"""Embedding generation via OpenAI-compatible embedding API.

Phase 3.5 — provider abstraction and embedding generation.
Vector storage in pgvector comes in Phase 3.6.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

import httpx

from erp_copilot.retrieval.ingestion import Chunk

_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 2.0


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Structural interface for embedding service providers."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for *texts*."""
        ...


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embedding API provider."""

    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int = 1536) -> None:
        base = base_url.rstrip("/")
        self._url = f"{base}/embeddings"
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        # 90s budget per attempt: DashScope stalls on 10-text batches at the
        # default 30s timeout.
        self._client = httpx.Client(timeout=httpx.Timeout(90.0))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        for batch_start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[batch_start : batch_start + _BATCH_SIZE]
            body: dict[str, object] = {"model": self._model, "input": batch}
            if self._dimensions:
                body["dimensions"] = self._dimensions

            all_embeddings.extend(self._embed_batch(batch, body))
        return all_embeddings

    def _embed_batch(self, batch: list[str], body: dict[str, object]) -> list[list[float]]:
        """POST one batch with retries for transient failures (429/5xx/transport)."""
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_status: int | str = "transport"
        last_detail: object = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.post(self._url, json=body, headers=headers)
            except httpx.TransportError as exc:
                last_detail = str(exc)
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                raise RuntimeError(f"Embedding API transport error: {exc}") from exc

            if response.status_code == 200:
                data = response.json()["data"]
                data.sort(key=lambda d: d["index"])
                return [d["embedding"] for d in data]

            try:
                last_detail = response.json()
            except Exception:
                last_detail = response.text
            last_status = response.status_code

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            break

        raise RuntimeError(f"Embedding API error {last_status}: {last_detail}")


# DashScope text-embedding-v4 batches of 10 time out; batches of 4 are stable.
_BATCH_SIZE = 4


def embed_chunks(
    chunks: list[Chunk],
    provider: EmbeddingProvider,
) -> list[tuple[Chunk, list[float]]]:
    """Generate embeddings for a list of Chunks.

    Returns a list of (chunk, embedding_vector) pairs in the same order.
    """
    if not chunks:
        return []

    texts = [c.content for c in chunks]
    embeddings = provider.embed(texts)
    return list(zip(chunks, embeddings, strict=True))
