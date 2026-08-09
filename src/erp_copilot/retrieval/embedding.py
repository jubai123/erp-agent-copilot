"""Embedding generation via OpenAI-compatible embedding API.

Phase 3.5 — provider abstraction and embedding generation.
Vector storage in pgvector comes in Phase 3.6.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from erp_copilot.retrieval.ingestion import Chunk


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
        self._client = httpx.Client(timeout=httpx.Timeout(30.0))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        # Some providers (DashScope) limit batch size — process in chunks of 10.
        all_embeddings: list[list[float]] = []
        for batch_start in range(0, len(texts), 10):
            batch = texts[batch_start : batch_start + 10]
            body: dict[str, object] = {"model": self._model, "input": batch}
            if self._dimensions:
                body["dimensions"] = self._dimensions

            response = self._client.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )

            if response.status_code != 200:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text
                raise RuntimeError(f"Embedding API error {response.status_code}: {detail}")

            data = response.json()["data"]
            data.sort(key=lambda d: d["index"])
            all_embeddings.extend(d["embedding"] for d in data)

        return all_embeddings


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
