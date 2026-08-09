"""Tests for embedding generation (Phase 3.5)."""

from __future__ import annotations

import json

import pytest

from erp_copilot.retrieval.embedding import (
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    embed_chunks,
)
from erp_copilot.retrieval.ingestion import Chunk

# ---------------------------------------------------------------------------
# Fake / stub embedding provider for tests
# ---------------------------------------------------------------------------


class _DummyProvider:
    """Returns fixed-dimension embeddings filled with a per-text offset."""

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [
            [float(i % self.dimensions) / self.dimensions for i in range(self.dimensions)]
            for _ in texts
        ]


class _FailingProvider:
    """Always raises so we can test error propagation."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Embedding service unavailable")


# ---------------------------------------------------------------------------
# Sample Chunk fixtures
# ---------------------------------------------------------------------------


def _make_chunk(chunk_id: str, content: str, index: int) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        content=content,
        source_document="test.md",
        section_path=["Test"],
        chunk_index=index,
        char_count=len(content),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmbedChunks:
    """Tests for embed_chunks with a stub provider."""

    def test_returns_correct_count(self) -> None:
        chunks = [_make_chunk("a", "hello", 0), _make_chunk("b", "world", 1)]
        provider = _DummyProvider(dimensions=128)
        result = embed_chunks(chunks, provider)
        assert len(result) == 2

    def test_embedding_dimensions_match_provider(self) -> None:
        chunks = [_make_chunk("a", "hello", 0)]
        provider = _DummyProvider(dimensions=256)
        result = embed_chunks(chunks, provider)
        assert len(result[0][1]) == 256

    def test_chunk_identity_preserved(self) -> None:
        chunks = [_make_chunk("id-1", "some text", 0)]
        provider = _DummyProvider(128)
        result = embed_chunks(chunks, provider)
        assert result[0][0].chunk_id == "id-1"
        assert result[0][0].content == "some text"

    def test_embedding_values_are_floats(self) -> None:
        chunks = [_make_chunk("a", "hello", 0)]
        provider = _DummyProvider(64)
        result = embed_chunks(chunks, provider)
        assert all(isinstance(v, float) for v in result[0][1])

    def test_passes_all_chunk_contents_to_provider(self) -> None:
        chunks = [
            _make_chunk("a", "first chunk", 0),
            _make_chunk("b", "second chunk", 1),
        ]
        provider = _DummyProvider(8)
        embed_chunks(chunks, provider)
        assert provider.calls[0] == ["first chunk", "second chunk"]

    def test_empty_input_returns_empty(self) -> None:
        result = embed_chunks([], _DummyProvider())
        assert result == []

    def test_failing_provider_propagates_error(self) -> None:
        chunks = [_make_chunk("a", "hello", 0)]
        with pytest.raises(RuntimeError, match="Embedding service unavailable"):
            embed_chunks(chunks, _FailingProvider())

    def test_result_order_matches_input(self) -> None:
        chunks = [
            _make_chunk("a", "alpha", 0),
            _make_chunk("b", "beta", 1),
            _make_chunk("c", "gamma", 2),
        ]
        provider = _DummyProvider(64)
        result = embed_chunks(chunks, provider)
        assert [r[0].chunk_id for r in result] == ["a", "b", "c"]


class TestOpenAIEmbeddingProvider:
    """Tests for the real HTTP-based provider (unit-tested via respx)."""

    def test_embed_returns_correct_dimensions(self, respx_mock) -> None:
        model = "text-embedding-3-small"
        provider = OpenAIEmbeddingProvider(
            base_url="https://test.openai.com/v1",
            api_key="sk-test",
            model=model,
        )

        respx_mock.post("https://test.openai.com/v1/embeddings").respond(
            json={
                "data": [
                    {"embedding": [0.1] * 512, "index": 0},
                    {"embedding": [0.2] * 512, "index": 1},
                ],
            },
        )

        result = provider.embed(["hello", "world"])
        assert len(result) == 2
        assert len(result[0]) == 512
        assert len(result[1]) == 512

    def test_embed_sends_correct_payload(self, respx_mock) -> None:
        model = "text-embedding-3-small"
        provider = OpenAIEmbeddingProvider(
            base_url="https://test.openai.com/v1",
            api_key="sk-abc",
            model=model,
        )

        route = respx_mock.post("https://test.openai.com/v1/embeddings").respond(
            json={"data": [{"embedding": [0.0], "index": 0}]},
        )

        provider.embed(["hello world"])
        request = route.calls.last.request
        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == ["hello world"]
        assert request.headers["Authorization"] == "Bearer sk-abc"

    def test_embed_empty_list_returns_empty(self, respx_mock) -> None:
        provider = OpenAIEmbeddingProvider(
            base_url="https://test.openai.com/v1",
            api_key="sk-test",
            model="test",
        )
        result = provider.embed([])
        assert result == []

    def test_http_error_propagates(self, respx_mock) -> None:
        provider = OpenAIEmbeddingProvider(
            base_url="https://test.openai.com/v1",
            api_key="sk-test",
            model="test",
        )
        respx_mock.post("https://test.openai.com/v1/embeddings").respond(
            status_code=429,
            json={"error": {"message": "Rate limit exceeded"}},
        )

        with pytest.raises(RuntimeError, match="Embedding API error"):
            provider.embed(["hello"])

    def test_conforms_to_protocol(self) -> None:
        """OpenAIEmbeddingProvider is structural subtype of EmbeddingProvider."""
        provider = OpenAIEmbeddingProvider("http://x", "k", "m")
        assert isinstance(provider, EmbeddingProvider)
