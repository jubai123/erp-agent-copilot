"""Deterministic intent classifier node — task 4.3.

Maps a natural-language query to a structured (domain, action) intent with an
initial risk level and explicit entities (docs/03 §4).  Deterministic by the
ADR rule "确定性优先，概率兜底": the (domain, action) output feeds two
downstream deterministic layers — skill_matcher (L1 skill injection) and
candidate_filter (tool candidate filtering) — both of which require exact key
hits, so a probabilistic classifier here would break their 100%-hit promise.

The entity catalogs (products / regions / units) are loaded at runtime from
datasets/knowledge/manifest.yaml through the vocabulary loader — the single
runtime authority — not hardcoded mirrors.  The order-status verb map stays
hardcoded: the manifest carries only the state enum, not the Chinese verbs.
An unrecognized query falls back to the lowest-risk read-only intent
(product/query) rather than raising, keeping the pipeline deterministic and
recoverable.  This pure function is the seam for a future LLM classifier: swap
its body, the node and graph topology stay unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from erp_copilot.agent.state import AgentState, AgentStatus, IntentClassification
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.vocabulary.loader import get_catalog, get_quantity_pattern

# order_id is a lowercase hex string (manifest order_fields says 12 chars; the
# planning dataset uses shorter ids like 3f2a9c1d / a1b2c3 / 20260809001).
_ORDER_ID_RE = re.compile(r"[0-9a-f]{6,12}")
_PRODUCT_ID_RE = re.compile(r"商品\s*(\d+)\s*号|编号\s*(\d+)")
# New quantity in a modify query ("数量从 10 改成 20" -> 20). Distinct from the
# loader-built quantity pattern, which keeps the *first* number as the original
# quantity.
_NEW_QUANTITY_RE = re.compile(r"(?:改成|改为)\s*(\d+)")

# Order-status verbs -> ERP status string (ordered: English tokens win, then
# the Chinese verb with the more specific meaning first).
_STATUS_MAP: tuple[tuple[str, str], ...] = (
    ("SHIPPED", "SHIPPED"),
    ("DELIVERED", "DELIVERED"),
    ("CONFIRMED", "CONFIRMED"),
    ("已发货", "SHIPPED"),
    ("确认收货", "DELIVERED"),
    ("确认订单", "CONFIRMED"),
)

# Intent rules in priority order — first match wins.  Keys stay in sync with
# datasets/knowledge/skills/intent_skill_map.yaml and candidate_filter.
_INTENT_RULES: tuple[tuple[tuple[str, ...], str, str, ToolRiskLevel], ...] = (
    (("权限", "脱敏", "数据访问"), "security", "data_access", ToolRiskLevel.DANGEROUS),
    (("场景",), "system", "scenario", ToolRiskLevel.READ),
    (("取消", "作废"), "order", "cancel", ToolRiskLevel.WRITE),
    (
        (
            "确认订单",
            "更新订单状态",
            "标记已发货",
            "标记为已发货",
            "确认收货",
            "已发货",
            "状态改为",
            "更新为",
            "改为",
            "SHIPPED",
            "DELIVERED",
            "CONFIRMED",
        ),
        "order",
        "update_status",
        ToolRiskLevel.WRITE,
    ),
    (("修改订单", "改单", "变更订单"), "order", "modify", ToolRiskLevel.WRITE),
    (("查订单", "查询订单", "订单状态", "订单详情"), "order", "query", ToolRiskLevel.READ),
    (
        ("下单", "下一单", "创建订单", "新建订单", "购买", "采购", "买"),
        "order",
        "create",
        ToolRiskLevel.WRITE,
    ),
    (("供应商", "物流", "配送", "发货"), "supplier", "query", ToolRiskLevel.READ),
    (("库存",), "product", "check_stock", ToolRiskLevel.READ),
)

# product/query doubles as the fallback: it is read-only (safest action) and
# product/query is not a rule of its own — the fallback returns it.
_FALLBACK: tuple[str, str, ToolRiskLevel] = ("product", "query", ToolRiskLevel.READ)


def _mask_order_ids(query: str) -> tuple[str, str | None]:
    """Return (query with order-id spans blanked, first order_id).

    Blanking prevents hex digits inside an order id from being re-parsed as a
    quantity or leaking into other entity extractors.
    """
    matches = list(_ORDER_ID_RE.finditer(query))
    if not matches:
        return query, None
    masked = list(query)
    for m in matches:
        for i in range(m.start(), m.end()):
            masked[i] = " "
    return "".join(masked), matches[0].group(0)


def _match_intent(query: str) -> tuple[str, str, ToolRiskLevel]:
    for keywords, domain, action, risk in _INTENT_RULES:
        if any(keyword in query for keyword in keywords):
            return domain, action, risk
    return _FALLBACK


def _extract_quantity(query: str) -> tuple[int | float | None, str | None]:
    match = get_quantity_pattern(get_catalog().units).search(query)
    if match is None:
        return None, None
    num_text = match.group(1)
    quantity: int | float = int(num_text) if "." not in num_text else float(num_text)
    return quantity, match.group(2)


def _extract_product_id(query: str) -> int | None:
    match = _PRODUCT_ID_RE.search(query)
    if match is None:
        return None
    return int(match.group(1) if match.group(1) is not None else match.group(2))


def _is_substitute(query: str) -> bool:
    return any(token in query for token in ("替代品", "类似", "相似"))


def _extract_status(query: str) -> str | None:
    for token, status in _STATUS_MAP:
        if token in query:
            return status
    return None


def _extract_new_quantity(query: str) -> int | None:
    match = _NEW_QUANTITY_RE.search(query)
    if match is None:
        return None
    return int(match.group(1))


def _extract_entities(query: str) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    catalog = get_catalog()
    for name in catalog.products:
        if name in query:
            entities["product"] = name
            break
    for region in catalog.regions:
        if region in query:
            entities["region"] = region
            break
    product_id = _extract_product_id(query)
    if product_id is not None:
        entities["product_id"] = product_id
    if _is_substitute(query):
        entities["substitute"] = True
    status = _extract_status(query)
    if status is not None:
        entities["status"] = status
    new_quantity = _extract_new_quantity(query)
    if new_quantity is not None:
        entities["new_quantity"] = new_quantity
    # product_id digits must not leak as quantity ("查商品 4 号" -> product_id
    # 4, not quantity 4).
    if "product_id" not in entities:
        quantity, unit = _extract_quantity(query)
        if quantity is not None:
            entities["quantity"] = quantity
        if unit is not None:
            entities["unit"] = unit
    return entities


def classify_intent(query: str) -> IntentClassification:
    """Deterministic (domain, action) classification with explicit entities."""
    masked, order_id = _mask_order_ids(query)
    domain, action, risk = _match_intent(masked)
    # A query that names a concrete order id but matched no rule is order
    # domain — e.g. "订单 7c9e1d5 的金额" (plan-018) falls to the fallback
    # otherwise and would be misread as a product query.
    if order_id is not None and (domain, action) == ("product", "query"):
        domain, action, risk = "order", "query", ToolRiskLevel.READ
    entities = _extract_entities(masked)
    if order_id is not None:
        entities["order_id"] = order_id
    return IntentClassification(domain=domain, action=action, risk_level=risk, entities=entities)


def domain_keywords(intent: IntentClassification) -> tuple[str, ...]:
    """Chinese search terms for the intent's (domain, action) rule.

    retrieve_context (task 4.4) uses these to build a focused L2 query from
    the intent instead of re-feeding the user's raw sentence. product/query —
    the fallback intent — has no rule of its own and yields an empty tuple.
    """
    for keywords, domain, action, _risk in _INTENT_RULES:
        if domain == intent.domain and action == intent.action:
            return keywords
    return ()


def classify_intent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node — classify the query and enter PLANNING."""
    return {"intent": classify_intent(state.query), "status": AgentStatus.PLANNING}
