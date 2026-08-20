"""Unit tests for vocabulary/extractor.py — LLM candidate extraction.

extract_candidates is the offline seam of the batch task: it takes an
injectable llm_complete, so tests stub the LLM with canned JSON and never hit
the network. The tests pin the strictness contract — unparseable output yields
[], wayward JSON keys are rejected (extra="forbid"), known terms and
low-confidence candidates are filtered, and units are normalized before dedup.
"""

from __future__ import annotations

import json

from erp_copilot.vocabulary.extractor import (
    build_extraction_prompt,
    extract_candidates,
    normalize_unit,
    proposal_key,
)
from erp_copilot.vocabulary.loader import VocabularyCatalog

_CATALOG = VocabularyCatalog(
    products=("苹果", "香蕉", "电脑"),
    regions=("上海", "北京"),
    units=("KG", "件"),
)
_KNOWN = {
    ("product", "苹果"),
    ("region", "上海"),
    ("unit", "KG"),
}


def _stub(payload: object):
    def llm_complete(prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    return llm_complete


class TestExtractCandidates:
    def test_parses_and_filters_known_and_confidence(self) -> None:
        payload = {
            "candidates": [
                {"canonical": "榴莲", "type": "product", "confidence": 0.9},
                {"canonical": "苏州", "type": "region", "confidence": 0.8},
                {"canonical": "苹果", "type": "product", "confidence": 0.9},  # known
                {"canonical": "京东", "type": "region", "confidence": 0.3},  # low
            ]
        }
        result = extract_candidates(
            _stub(payload), ["榴莲多少钱"], _CATALOG, _KNOWN
        )
        assert [(c.canonical, c.type) for c in result] == [("榴莲", "product"), ("苏州", "region")]

    def test_unparseable_response_returns_empty(self) -> None:
        def llm_complete(prompt: str) -> str:
            return "not json at all"

        assert extract_candidates(llm_complete, ["榴莲多少钱"], _CATALOG, _KNOWN) == []

    def test_missing_candidates_key_returns_empty(self) -> None:
        assert extract_candidates(_stub({"proposals": []}), ["榴莲多少钱"], _CATALOG, _KNOWN) == []

    def test_extra_key_rejected(self) -> None:
        payload = {
            "candidates": [
                {
                    "canonical": "榴莲",
                    "type": "product",
                    "confidence": 0.9,
                    "oops": "wayward",
                }
            ]
        }
        assert extract_candidates(_stub(payload), ["榴莲多少钱"], _CATALOG, _KNOWN) == []

    def test_invalid_candidate_skipped(self) -> None:
        payload = {
            "candidates": [
                {"canonical": "榴莲", "type": "car", "confidence": 0.9},  # bad type
                {"confidence": 0.9},  # missing canonical
            ]
        }
        assert extract_candidates(_stub(payload), ["榴莲多少钱"], _CATALOG, _KNOWN) == []

    def test_units_normalized_against_known(self) -> None:
        # "kg" lowercases to KG, which is already known -> filtered.
        payload = {"candidates": [{"canonical": "kg", "type": "unit", "confidence": 0.9}]}
        assert extract_candidates(_stub(payload), ["下单一斤"], _CATALOG, _KNOWN) == []

    def test_duplicates_within_batch_deduped(self) -> None:
        payload = {
            "candidates": [
                {"canonical": "榴莲", "type": "product", "confidence": 0.9},
                {"canonical": "榴莲", "type": "product", "confidence": 0.8},
            ]
        }
        result = extract_candidates(_stub(payload), ["榴莲多少钱"], _CATALOG, _KNOWN)
        assert len(result) == 1

    def test_empty_queries_returns_empty(self) -> None:
        assert extract_candidates(_stub({}), [], _CATALOG, _KNOWN) == []

    def test_evidence_and_aliases_passed_through(self) -> None:
        payload = {
            "candidates": [
                {
                    "canonical": "榴莲",
                    "type": "product",
                    "confidence": 0.7,
                    "aliases": ["榴莲果"],
                    "evidence": ["榴莲多少钱"],
                }
            ]
        }
        (candidate,) = extract_candidates(_stub(payload), ["榴莲多少钱"], _CATALOG, _KNOWN)
        assert candidate.aliases == ["榴莲果"]
        assert candidate.evidence == ["榴莲多少钱"]


class TestHelpers:
    def test_normalize_unit_only_uppercases_units(self) -> None:
        assert normalize_unit("kg", "unit") == "KG"
        assert normalize_unit("榴莲", "product") == "榴莲"
        assert normalize_unit("苏州", "region") == "苏州"

    def test_proposal_key_normalizes_unit(self) -> None:
        assert proposal_key("unit", "kg") == proposal_key("unit", "KG")
        assert proposal_key("product", "榴莲") != proposal_key("unit", "榴莲")

    def test_prompt_includes_known_catalog(self) -> None:
        prompt = build_extraction_prompt(["榴莲多少钱"], _CATALOG)
        assert "苹果" in prompt
        assert "上海" in prompt
        assert "KG" in prompt
        assert '"candidates"' in prompt
