"""Three-layer funnel router — decides which layer serves a query.

The funnel (session 46 ADR): Tier1 deterministic fast path
(classify_intent → build_plan_from_intent), Tier2 LLM constrained planning
(skill catalog injected prompt, build_plan_node), Tier3 low-confidence free
planning. This module is the *decision*: :func:`route_query_layer` returns the
layer a query belongs to, consumed by the routing eval as the single source of
truth for the funnel's honest boundary.

The boundary rule: Tier1 only serves queries the deterministic grammar can
express *faithfully*. The deterministic chain never raises — an out-of-scope
query silently falls through to a plausible-but-wrong plan (the overgrab
hazard: "苏州有哪些供应商" grabs getSupplierByStatus, "苹果和香蕉哪个贵"
grabs getProductByName). The router compensates with two deterministic
signals:

- **complexity** — a query that expresses comparison / condition / multi-intent
  / multi-entity / product-supplier association / fuzzy / time reasoning is
  beyond the grammar's expressive power and routes straight to tier3,
  regardless of what plan the chain would produce.
- **uncovered region** — a supplier query scoped by a real city outside the
  classifier's delivery vocabulary (``_REGIONS``) cannot be answered honestly
  by tier1; route to tier2.

Every trigger keyword below is pinned against the 50-case routing oracle
(evals/datasets/agent_routing_50.json) by tests/unit/agent/test_routing.py: the
20 tier1 queries must not trip any gate, the 30 tier2/3 queries must route up.
The gates are a deliberately hand-authored boundary vocabulary — they measure
honest boundary behaviour on the authored oracle, not real-world recall.
"""

from __future__ import annotations

import re
from typing import Literal

from erp_copilot.agent.nodes.classify_intent import (
    _PRODUCT_NAMES,
    _REGIONS,
    classify_intent,
)
from erp_copilot.agent.planner import build_plan_from_intent
from erp_copilot.agent.state import IntentClassification

Layer = Literal["tier1", "tier2", "tier3"]

# Beyond-grammar triggers: any hit means the query needs tier3 free planning.
# "推荐一" (not bare "推荐") and "性价比" (not bare "推荐") so the tier1 case
# "查苹果库存并推荐供应商" stays tier1 — 推荐供应商 is a defined action.
_COMPARISON: tuple[str, ...] = (
    "对比",
    "比较",
    "哪个贵",
    "哪个便宜",
    "贵多少",
    "便宜多少",
    "性价比",
)
_COMPARISON_BI_RE = re.compile(r"比.{1,6}(贵|便宜)")
_CONDITION: tuple[str, ...] = ("如果", "只要", "换成", "卖完")
# The order id between 先 and 再 stretches the span ("先取消订单 3f2a9c1d 再…").
_MULTI_INTENT_RE = re.compile(r"先.{0,20}再")
# Product-supplier association is "X的供应商" with X a product — NOT the
# grammatical particle in "正常状态的供应商" (a status query, tier1).
_PRODUCT_ASSOCIATION_RE = re.compile(r"(?:" + "|".join(_PRODUCT_NAMES) + r")的供应商")
_ASSOCIATION: tuple[str, ...] = ("供应商是哪家", "供应商是谁")
_FUZZY: tuple[str, ...] = ("能不能", "分期", "推荐一", "安排", "计划")
_TIME: tuple[str, ...] = ("今天", "明天", "后天", "本周", "下周", "近期")

# Real cities the classifier's _REGIONS vocabulary does not cover. A supplier
# query scoped by one of these cannot be answered by the deterministic layer —
# which would silently grab getSupplierByStatus — so route to tier2.
_UNCOVERED_CITY_HINTS: tuple[str, ...] = (
    "苏州",
    "武汉",
    "杭州",
    "郑州",
    "长沙",
    "青岛",
    "厦门",
    "大连",
    "昆明",
    "合肥",
)


def _complexity(query: str) -> Layer | None:
    """Return "tier3" when *query* expresses something beyond the grammar."""
    if any(token in query for token in _COMPARISON) or _COMPARISON_BI_RE.search(query):
        return "tier3"
    if any(token in query for token in _CONDITION):
        return "tier3"
    if _MULTI_INTENT_RE.search(query):
        return "tier3"
    if sum(1 for product in _PRODUCT_NAMES if product in query) >= 2:
        return "tier3"
    if sum(1 for region in _REGIONS if region in query) >= 2:
        return "tier3"
    if _PRODUCT_ASSOCIATION_RE.search(query) or any(token in query for token in _ASSOCIATION):
        return "tier3"
    if any(token in query for token in _FUZZY) or any(token in query for token in _TIME):
        return "tier3"
    return None


def _unknown_region(query: str, intent: IntentClassification) -> bool:
    """True when a supplier query is scoped by a city outside _REGIONS."""
    return (
        intent.domain == "supplier"
        and intent.action == "query"
        and any(city in query for city in _UNCOVERED_CITY_HINTS)
    )


def route_query_layer(query: str) -> Layer:
    """Decide which funnel layer should serve *query* (pure, offline, deterministic).

    Order matters: complexity is decided before trusting the deterministic
    plan, so a beyond-grammar query never lands in tier1 no matter what plan the
    chain happens to produce. An honest EMPTY_PLAN refusal defaults to tier2
    (LLM constrained planning can usually complete it); only an uncovered region
    in a supplier query upgrades that same refusal-and-grab path to tier2 before
    tier1's default claim.
    """
    complexity = _complexity(query)
    if complexity is not None:
        return complexity
    intent = classify_intent(query)
    plan, errors = build_plan_from_intent(intent)
    if errors or plan is None or not plan.steps:
        return "tier2"
    if _unknown_region(query, intent):
        return "tier2"
    return "tier1"
