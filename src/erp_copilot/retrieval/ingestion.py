"""Document ingestion: Markdown parsing and text chunking.

Phase 3.3 — no database writes, no embedding.  Pure parsing and chunking
with metadata assembly.  Deduplication and version management are added
in Phase 3.4.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Chunk:
    """A text chunk with source and section metadata."""

    chunk_id: str
    content: str
    source_document: str
    section_path: list[str]
    chunk_index: int
    char_count: int


# ---------------------------------------------------------------------------
# Markdown section parser
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+\s*)?$")
_INLINE_FORMAT_RE = re.compile(r"[`*_]{1,3}")


def _clean_heading(text: str) -> str:
    """Strip inline Markdown formatting from a heading."""
    return _INLINE_FORMAT_RE.sub("", text).strip()


def parse_markdown_sections(text: str) -> list[dict]:
    """Parse Markdown text into sections with heading hierarchy.

    Each returned dict has:
      - heading_path: list of ancestor heading texts (e.g. ["H1", "H2"])
      - content: the body text under that heading

    Headings inside fenced code blocks (```) are ignored.
    Content before the first heading gets an empty heading_path.
    """
    lines = text.split("\n")
    sections: list[dict] = []

    heading_stack: list[tuple[int, str]] = []  # (level, cleaned_text)
    body_lines: list[str] = []
    in_code_block = False

    for line in lines:
        # Toggle code-block state on ``` fences
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            body_lines.append(line)
            continue

        # Heading detection — only outside code blocks
        if not in_code_block:
            m = _HEADING_RE.match(line)
            if m:
                # Flush accumulated body as previous section
                content = "\n".join(body_lines).strip()
                if content:
                    sections.append({
                        "heading_path": [h[1] for h in heading_stack],
                        "content": content,
                    })
                body_lines = []

                level = len(m.group(1))
                heading_text = _clean_heading(m.group(2))

                # Pop headings at same-or-deeper level
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, heading_text))
                continue

        body_lines.append(line)

    # Flush trailing section
    content = "\n".join(body_lines).strip()
    if content:
        sections.append({
            "heading_path": [h[1] for h in heading_stack],
            "content": content,
        })

    return sections


# ---------------------------------------------------------------------------
# Sliding-window chunker
# ---------------------------------------------------------------------------


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split *text* into overlapping chunks of at most *chunk_size* characters.

    *overlap* characters repeat at the start of each consecutive chunk.
    When *overlap* ≥ *chunk_size* it is clamped to half the chunk size.
    """
    text = text.strip()
    if not text:
        return []

    effective_overlap = min(overlap, chunk_size // 2)
    step = max(chunk_size - effective_overlap, 1)

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunks.append(text[start:end])
        if end >= text_len:
            break
        start += step

    return chunks


# ---------------------------------------------------------------------------
# Full ingestion pipeline
# ---------------------------------------------------------------------------


def ingest_document(
    content: str,
    source: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """Parse *content* and chunk it, returning Chunk objects with metadata.

    Markdown content is first split into sections by heading; plain text
    is treated as a single section.  Each section is then window-chunked.
    """
    stripped = content.strip()
    if not stripped:
        return []

    sections = parse_markdown_sections(stripped)
    if not sections:
        return []

    result: list[Chunk] = []
    global_index = 0

    for section in sections:
        for chunk_content in chunk_text(section["content"], chunk_size, overlap):
            result.append(Chunk(
                chunk_id=str(uuid.uuid4()),
                content=chunk_content,
                source_document=source,
                section_path=list(section["heading_path"]),
                chunk_index=global_index,
                char_count=len(chunk_content),
            ))
            global_index += 1

    return result


# ---------------------------------------------------------------------------
# Content hashing and deduplication (Phase 3.4)
# ---------------------------------------------------------------------------


def hash_content(content: str) -> str:
    """Return the SHA-256 hex digest of *content*."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def find_existing_document(
    session: Session,
    tenant_id: str,
    content_hash: str,
):
    """Return the KnowledgeDocument with the same hash in *tenant_id*, or None."""
    from erp_copilot.domain.entities import KnowledgeDocument

    return (
        session.query(KnowledgeDocument)
        .filter(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.content_hash == content_hash,
        )
        .first()
    )
