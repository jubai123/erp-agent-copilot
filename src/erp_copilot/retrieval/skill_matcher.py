"""Deterministic L1 skill matcher — no vector search, no LLM involvement.

Maps (domain, action) pairs to a fixed list of skills via a lookup table
(intent_skill_map.yaml).  Skills themselves are defined in skills.yaml.

This is the L1 path of the two-tier knowledge architecture (ADR decision 2).
L1 skills are injected into the build_plan prompt before L2 retrieved context.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SKILLS_DIR = _ROOT / "datasets" / "knowledge" / "skills"


def _load_skills() -> dict[str, dict]:
    path = _SKILLS_DIR / "skills.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return {item["skill_id"]: item for item in raw}


def _load_intent_map() -> dict[tuple[str, str], list[str]]:
    path = _SKILLS_DIR / "intent_skill_map.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    mapping: dict[tuple[str, str], list[str]] = {}
    for entry in raw["intents"]:
        key = (entry["domain"], entry["action"])
        mapping[key] = entry["skills"]
    return mapping


_SKILLS_CACHE: dict[str, dict] | None = None
_INTENT_MAP_CACHE: dict[tuple[str, str], list[str]] | None = None


def _get_skills() -> dict[str, dict]:
    global _SKILLS_CACHE
    if _SKILLS_CACHE is None:
        _SKILLS_CACHE = _load_skills()
    return _SKILLS_CACHE


def _get_intent_map() -> dict[tuple[str, str], list[str]]:
    global _INTENT_MAP_CACHE
    if _INTENT_MAP_CACHE is None:
        _INTENT_MAP_CACHE = _load_intent_map()
    return _INTENT_MAP_CACHE


def match_skills(domain: str, action: str) -> list[dict]:
    """Return the L1 skills matched to an intent.

    Deterministic lookup — never uses vector search or an LLM.
    Returns an empty list when the intent is unknown (no crash).
    """
    intent_map = _get_intent_map()
    skill_ids = intent_map.get((domain, action), [])
    if not skill_ids:
        return []

    skills = _get_skills()
    result: list[dict] = []
    for sid in skill_ids:
        skill = skills.get(sid)
        if skill is not None:
            result.append(dict(skill))
    return result
