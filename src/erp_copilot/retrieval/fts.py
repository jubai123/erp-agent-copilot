"""Full-text keyword search via PostgreSQL tsvector.

Phase 3.7 — tsvector + GIN index on document_chunks.content.
Uses jieba segmentation so Chinese text is tokenized before
``to_tsvector('simple', ...)`` indexing and querying.
"""

from __future__ import annotations

import json

import jieba
from sqlalchemy import text
from sqlalchemy.orm import Session


def jieba_segment(text: str) -> str:
    """Segment Chinese text with jieba, returning space-joined tokens.

    Mixed Chinese/ASCII text is handled correctly: jieba tokenizes CJK
    characters while leaving ASCII words (English, code, numbers) intact.
    """
    if not text or not text.strip():
        return ""
    tokens = jieba.cut(text.strip())
    return " ".join(tokens)


# Chinese function words / question particles that never carry retrieval
# signal. Dropped before building the tsquery so an OR-match doesn't fire
# on "如何/怎么/哪些" and inflate ts_rank with spurious hits.
_STOPWORDS = frozenset({
    "如何", "怎么", "怎样", "哪些", "什么", "什么样", "为什么",
    "什么时候", "怎么办", "为何", "是否", "谁", "吗", "呢", "啊", "吧",
    "的", "了", "在", "有", "是", "会", "不会", "都", "所有", "这个",
    "那个", "一个", "同一个", "可以", "需要", "能够", "按", "到", "为",
    "用", "包括", "及", "和", "与", "或", "等", "时", "后", "中", "下",
    "于", "对", "从", "就", "很", "再", "也", "还", "才", "又", "多少",
    "还有", "只有", "没有",
})


def _is_retrieval_stop(token: str) -> bool:
    """Return True if *token* carries no keyword-search signal.

    Two classes are dropped: explicit stopwords, and single CJK
    characters (e.g. 幂/字/段) that jieba fragments but that never
    appear as standalone terms in the knowledge base.
    """
    if token in _STOPWORDS:
        return True
    if len(token) == 1 and "一" <= token <= "鿿":
        return True
    return False


def keyword_search(
    session: Session,
    query: str,
    tenant_id: str,
    top_k: int = 10,
) -> list[dict]:
    """Search chunks by keyword using PostgreSQL full-text search.

    Tenant isolation is enforced by joining through ``knowledge_documents``.
    Results are ordered by ``ts_rank`` descending.

    Returns a list of dicts with keys: chunk_id, content, section_path,
    char_count, source, rank.
    """
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must not be empty")

    # Segment, then drop stopwords so the OR-match only fires on terms
    # that actually appear in technical documents.
    segmented = jieba_segment(stripped)
    tokens = [t for t in segmented.split() if not _is_retrieval_stop(t)]
    if not tokens:
        return []

    # OR semantics: at least one token must match. plainto_tsquery's AND
    # behaviour never matched real queries because question words
    # (如何/怎么/哪些) don't appear in technical docs. ts_rank then orders
    # chunks matching more terms higher.
    tsquery = " | ".join(tokens)

    rows = session.execute(
        text("""
            SELECT
                dc.id          AS chunk_id,
                dc.content     AS content,
                dc.section_path AS section_path,
                dc.char_count  AS char_count,
                kd.source      AS source,
                ts_rank(dc.search_vector, to_tsquery('simple', :query)) AS rank
            FROM document_chunks dc
            JOIN knowledge_documents kd ON kd.id = dc.document_id
            WHERE kd.tenant_id = :tenant_id
              AND dc.search_vector @@ to_tsquery('simple', :query)
            ORDER BY rank DESC
            LIMIT :top_k
        """),
        {
            "query": tsquery,
            "tenant_id": tenant_id,
            "top_k": top_k,
        },
    ).mappings().all()

    return [
        {
            "chunk_id": r["chunk_id"],
            "content": r["content"],
            "section_path": json.loads(r["section_path"]),
            "char_count": r["char_count"],
            "source": r["source"],
            "rank": r["rank"],
        }
        for r in rows
    ]
