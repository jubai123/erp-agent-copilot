"""LLM batch extraction of vocabulary candidates from captured OOV queries.

The capture half records queries that carried no known product/region/unit
entity; this module turns a batch of those into validated candidates that a
Celery task persists as PENDING VocabularyProposal rows for human approval.
The LLM output is never authoritative: candidates stay PENDING until an
operator approves them (service.py writes vocabulary_terms).

LLM output is validated strictly: extra="forbid" rejects wayward JSON keys, an
unparseable response yields [] instead of raising, and known terms /
low-confidence candidates are filtered before persisting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from erp_copilot.vocabulary.loader import VocabularyCatalog

VocabType = Literal["product", "region", "unit"]


class VocabularyCandidate(BaseModel):
    """A single LLM-suggested term; extra="forbid" rejects wayward JSON keys."""

    model_config = ConfigDict(extra="forbid")

    canonical: str
    type: VocabType
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


def normalize_unit(canonical: str, vocab_type: str) -> str:
    """Uppercase unit canonical (KG vs kg are the same catalog entry)."""
    return canonical.upper() if vocab_type == "unit" else canonical


def proposal_key(vocab_type: str, canonical: str) -> str:
    """Idempotency key: sha1 of type + normalized canonical."""
    normalized = normalize_unit(canonical, vocab_type)
    return hashlib.sha1(f"{vocab_type}|{normalized}".encode()).hexdigest()


def build_extraction_prompt(
    queries: Sequence[str],
    catalog: VocabularyCatalog,
) -> str:
    """Prompt the LLM to extract vocabulary candidates from *queries*.

    Injects the current known catalogs so the LLM does not re-suggest existing
    terms, and demands a strict JSON object {"candidates": [...]}.
    """
    known = "\n".join(
        (
            f"- 商品: {', '.join(catalog.products)}",
            f"- 区域: {', '.join(catalog.regions)}",
            f"- 单位: {', '.join(catalog.units)}",
        )
    )
    quoted = "\n".join(f"- {q}" for q in queries)
    return (
        "你是 ERP 词表分析助手。下面是从用户真实查询中采集到的、当前词表无法识别的查询。\n\n"
        f"当前已知词表：\n{known}\n\n"
        "请从这些查询中提取可能的新词条（商品 / 区域 / 单位），即用户在表达"
        "业务意图时使用的实义词。规则：\n"
        "1. 忽略已知词表中的词。\n"
        "2. 忽略纯表达性、非词条性内容（如问候、疑问句式）。\n"
        "3. 每个候选词给出：canonical（规范写法）、type（product / region / unit）、"
        "aliases（可能的别名列表）、confidence（0-1 的置信度）、evidence（包含该词的原始查询）。\n"
        f"4. 只输出一个 JSON 对象：{{\"candidates\": [...]}}。不要输出其他文字。\n\n"
        f"待分析查询：\n{quoted}\n"
    )


def extract_candidates(
    llm_complete: Callable[[str], str],
    queries: Sequence[str],
    catalog: VocabularyCatalog,
    known: set[tuple[str, str]],
    *,
    min_confidence: float = 0.5,
) -> list[VocabularyCandidate]:
    """Extract + filter candidates from *queries* via *llm_complete*.

    Pure (no DB). Returns validated candidates that are not already known and
    clear the confidence floor, deduped by (type, normalized canonical). A
    malformed or unparseable LLM response returns [] rather than raising, so
    the batch task never crashes on a wayward model.
    """
    if not queries:
        return []
    raw = llm_complete(build_extraction_prompt(queries, catalog))
    try:
        payload = json.loads(raw)
        items = payload["candidates"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    seen = set(known)
    candidates: list[VocabularyCandidate] = []
    for item in items:
        try:
            candidate = VocabularyCandidate.model_validate(item)
        except Exception:
            continue
        if candidate.confidence < min_confidence:
            continue
        key = (candidate.type, normalize_unit(candidate.canonical, candidate.type))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates
