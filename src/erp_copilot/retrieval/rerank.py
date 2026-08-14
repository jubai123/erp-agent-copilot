"""Cross-encoder reranking for precision-focused retrieval.

Phase 3.9 — re-ranks fused results from :func:`rrf_fuse` using a
cross-encoder model that processes (query, document) pairs jointly.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

import httpx

_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 2.0


@runtime_checkable
class Reranker(Protocol):
    """Structural interface for reranking providers."""

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """Return (index, relevance_score) pairs sorted by score descending."""
        ...


class DashScopeReranker:
    """Reranker via DashScope (通义千问) native rerank API.

    Uses the ``qwen3-rerank`` model — 通义千问3 的重排序模型，中文
    语义匹配能力强。需要 DashScope API Key（DASHSCOPE_API_KEY）。

    The DashScope rerank endpoint uses the native API format (not the
    OpenAI-compatible mode).
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "qwen3-rerank",
    ) -> None:
        self._url = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
        self._api_key = api_key
        self._model = model
        # 90s budget per attempt: reranking ~20 candidate chunks takes
        # seconds, but the DashScope endpoint occasionally stalls.
        self._client = httpx.Client(timeout=httpx.Timeout(90.0))

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not documents:
            return []

        payload = {
            "model": self._model,
            "input": {
                "query": query,
                "documents": documents,
            },
            "parameters": {
                "top_n": len(documents),
                "return_documents": False,
            },
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_status: int | str = "transport"
        last_detail: object = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.post(self._url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                last_detail = str(exc)
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                raise RuntimeError(f"Rerank API transport error: {exc}") from exc

            if response.status_code == 200:
                results = response.json()["output"]["results"]
                scored = [(r["index"], float(r["relevance_score"])) for r in results]
                scored.sort(key=lambda x: x[1], reverse=True)
                return scored

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

        raise RuntimeError(f"Rerank API error {last_status}: {last_detail}")


class CrossEncoderReranker:
    """Reranker backed by a sentence-transformers cross-encoder model.

    Uses *model_name* (e.g. ``BAAI/bge-reranker-v2-m3``) to compute
    joint query-document relevance scores.
    """

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not documents:
            return []

        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(pairs, show_progress_bar=False)
        scored = [
            (i, float(scores[i] if isinstance(scores, list) else scores[i].item()))
            for i in range(len(documents))
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


def rerank_results(
    query: str,
    documents: list[dict],
    reranker: Reranker,
    top_k: int = 5,
) -> list[dict]:
    """Re-rank *documents* using *reranker* and return top_k results.

    Each document dict must have a ``content`` key.  The output dicts
    drop internal scoring fields (rrf_score, distance, rank) and add a
    ``rerank_score`` key.
    """
    if not documents:
        return []

    contents = [d["content"] for d in documents]
    scored = reranker.rerank(query, contents)

    result = []
    for idx, score in scored[:top_k]:
        d = documents[idx]
        result.append(
            {
                "chunk_id": d["chunk_id"],
                "content": d["content"],
                "section_path": d["section_path"],
                "char_count": d["char_count"],
                "source": d["source"],
                "rerank_score": score,
            }
        )

    return result
