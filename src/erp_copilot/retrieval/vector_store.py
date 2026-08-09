"""Vector storage and similarity search via pgvector.

Phase 3.6 — stores chunk embeddings and queries by cosine distance.
Also updates the FTS search_vector with jieba-segmented content so
Chinese full-text search works correctly.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from erp_copilot.retrieval.fts import jieba_segment


def store_embeddings(
    session: Session,
    document_id: str,
    chunks_with_embeddings: list[tuple],
) -> None:
    """Persist embeddings to the document_chunks table.

    *chunks_with_embeddings* is a list of (Chunk, list[float]) pairs as
    returned by :func:`~erp_copilot.retrieval.embedding.embed_chunks`.

    After insertion, the FTS search_vector is overwritten with a
    jieba-segmented version so Chinese queries can match individual words.
    """
    if not chunks_with_embeddings:
        return

    params = [
        {
            "chunk_id": chunk.chunk_id,
            "document_id": document_id,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "section_path": json.dumps(chunk.section_path),
            "char_count": chunk.char_count,
            "embedding": embedding,
        }
        for chunk, embedding in chunks_with_embeddings
    ]

    session.execute(
        text("""
            INSERT INTO document_chunks (
                id, document_id, chunk_index, content,
                section_path, char_count, embedding, created_at
            )
            VALUES (
                :chunk_id, :document_id, :chunk_index, :content,
                :section_path, :char_count, :embedding, now()
            )
            ON CONFLICT (document_id, chunk_index) DO UPDATE SET
                embedding = EXCLUDED.embedding
        """),
        params,
    )

    # Overwrite search_vector with jieba-segmented content for Chinese FTS.
    # The DB trigger auto-generates search_vector from raw content, but
    # with 'simple' config Chinese text is treated as a single token.
    # Jieba segmentation produces space-separated tokens that the trigger
    # can properly index.
    segmented_updates = [
        {"chunk_id": chunk.chunk_id, "segmented": jieba_segment(chunk.content)}
        for chunk, _ in chunks_with_embeddings
    ]
    segmented_updates = [u for u in segmented_updates if u["segmented"]]

    if segmented_updates:
        session.execute(
            text("""
                UPDATE document_chunks
                SET search_vector = to_tsvector('simple', :segmented)
                WHERE id = :chunk_id
            """),
            segmented_updates,
        )

    session.flush()


def search_similar(
    session: Session,
    query_embedding: list[float],
    tenant_id: str,
    top_k: int = 10,
) -> list[dict]:
    """Return the *top_k* most similar chunks by cosine distance.

    Tenant isolation is enforced by joining through ``knowledge_documents``.
    Returns a list of dicts with keys: chunk_id, content, section_path,
    char_count, source, distance.
    """
    if not query_embedding:
        raise ValueError("query_embedding must not be empty")

    embedding_str = json.dumps(query_embedding)

    rows = session.execute(
        text(r"""
            SELECT
                dc.id          AS chunk_id,
                dc.content     AS content,
                dc.section_path AS section_path,
                dc.char_count  AS char_count,
                kd.source      AS source,
                dc.embedding <=> :embedding\:\:vector AS distance
            FROM document_chunks dc
            JOIN knowledge_documents kd ON kd.id = dc.document_id
            WHERE kd.tenant_id = :tenant_id
              AND dc.embedding IS NOT NULL
            ORDER BY dc.embedding <=> :embedding\:\:vector
            LIMIT :top_k
        """),
        {
            "embedding": embedding_str,
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
            "distance": r["distance"],
        }
        for r in rows
    ]
