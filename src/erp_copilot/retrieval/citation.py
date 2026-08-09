"""Citation assembly for LLM prompt injection.

Phase 3.10 — formats retrieval results as numbered reference blocks
that LLMs can cite in their responses.
"""

from __future__ import annotations


def assemble_citations(results: list[dict]) -> str:
    """Format *results* as a numbered citation block in Markdown.

    Each result dict should have keys: content, source, section_path.

    Returns an empty string when *results* is empty.
    """
    if not results:
        return ""

    lines = ["## 参考资料\n"]

    for i, r in enumerate(results, start=1):
        source_path = _format_source(r["source"], r.get("section_path", []))
        lines.append(f"[{i}] 来源: {source_path}")
        lines.append(r["content"])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format_source(source: str, section_path: list[str]) -> str:
    if section_path:
        return f"{source} > {' > '.join(section_path)}"
    return source
