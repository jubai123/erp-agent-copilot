"""Deterministic L1 rule matcher — no vector search, no LLM involvement.

Maps (domain, action) pairs to a fixed list of rules via a lookup table
(intent_rule_map.yaml).  Rules themselves are defined in rules.yaml.

This is the L1 path of the two-tier knowledge architecture (ADR decision 2).
L1 rules are injected into the build_plan prompt before L2 retrieved context.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RULES_DIR = _ROOT / "datasets" / "knowledge" / "rules"


def _load_rules() -> dict[str, dict]:
    path = _RULES_DIR / "rules.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return {item["rule_id"]: item for item in raw}


def _load_intent_map() -> dict[tuple[str, str], list[str]]:
    path = _RULES_DIR / "intent_rule_map.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    mapping: dict[tuple[str, str], list[str]] = {}
    for entry in raw["intents"]:
        key = (entry["domain"], entry["action"])
        mapping[key] = entry["rules"]
    return mapping


_RULES_CACHE: dict[str, dict] | None = None
_INTENT_MAP_CACHE: dict[tuple[str, str], list[str]] | None = None


def _get_rules() -> dict[str, dict]:
    global _RULES_CACHE
    if _RULES_CACHE is None:
        _RULES_CACHE = _load_rules()
    return _RULES_CACHE


def _get_intent_map() -> dict[tuple[str, str], list[str]]:
    global _INTENT_MAP_CACHE
    if _INTENT_MAP_CACHE is None:
        _INTENT_MAP_CACHE = _load_intent_map()
    return _INTENT_MAP_CACHE


def match_rules(domain: str, action: str) -> list[dict]:
    """Return the L1 rules matched to an intent.

    Deterministic lookup — never uses vector search or an LLM.
    Returns an empty list when the intent is unknown (no crash).
    """
    intent_map = _get_intent_map()
    rule_ids = intent_map.get((domain, action), [])
    if not rule_ids:
        return []

    rules = _get_rules()
    result: list[dict] = []
    for rid in rule_ids:
        rule = rules.get(rid)
        if rule is not None:
            result.append(dict(rule))
    return result
