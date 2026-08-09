"""Cross-encoder reranking for precision-focused retrieval.

Phase 3.9 — re-ranks fused results from :func:`rrf_fuse` using a
cross-encoder model that processes (query, document) pairs jointly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


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
        import httpx

        self._url = (
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/"
            "text-rerank/text-rerank"
        )
        self._api_key = api_key
        self._model = model
        self._client = httpx.Client(timeout=httpx.Timeout(30.0))

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not documents:
            return []

        response = self._client.post(
            self._url,
            json={
                "model": self._model,
                "input": {
                    "query": query,
                    "documents": documents,
                },
                "parameters": {
                    "top_n": len(documents),
                    "return_documents": False,
                },
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

        if response.status_code != 200:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(
                f"Rerank API error {response.status_code}: {detail}"
            )

        results = response.json()["output"]["results"]
        scored = [(r["index"], float(r["relevance_score"])) for r in results]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


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
        result.append({
            "chunk_id": d["chunk_id"],
            "content": d["content"],
            "section_path": d["section_path"],
            "char_count": d["char_count"],
            "source": d["source"],
            "rerank_score": score,
        })

    return result
