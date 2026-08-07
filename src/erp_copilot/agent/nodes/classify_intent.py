"""Deterministic intent classifier node — task 4.3.

Maps a natural-language query to a structured (domain, action) intent with an
initial risk level and explicit entities (docs/03 §4).  Deterministic by the
ADR rule "确定性优先，概率兜底": the (domain, action) output feeds two
downstream deterministic layers — skill_matcher (L1 skill injection) and
candidate_filter (tool candidate filtering) — both of which require exact key
hits, so a probabilistic classifier here would break their 100%-hit promise.

The entity catalogs below mirror datasets/knowledge/manifest.yaml, the
authority for product / region / unit / order-id formats.  An unrecognized
query falls back to the lowest-risk read-only intent (product/query) rather
than raising, keeping the pipeline deterministic and recoverable.  This pure
function is the seam for a future LLM classifier: swap its body, the node and
graph topology stay unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from erp_copilot.agent.state import AgentState, AgentStatus, IntentClassification
from erp_copilot.domain.enums import ToolRiskLevel

# Entity catalogs — mirror datasets/knowledge/manifest.yaml.
_PRODUCT_NAMES: tuple[str, ...] = ("苹果", "香蕉", "橙子", "电脑", "键盘", "鼠标")
_REGIONS: tuple[str, ...] = (
    "上海",
    "南京",
    "北京",
    "天津",
    "广州",
    "深圳",
    "成都",
    "重庆",
    "西安",
    "兰州",
)
_UNITS: tuple[str, ...] = ("KG", "台", "件")

# order_id is a 12-char lowercase hex string (manifest order_fields).
_ORDER_ID_RE = re.compile(r"[0-9a-f]{12}")
_QUANTITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + "|".join(_UNITS) + r")?")

# Intent rules in priority order — first match wins.  Keys stay in sync with
# datasets/knowledge/skills/intent_skill_map.yaml and candidate_filter.
_INTENT_RULES: tuple[tuple[tuple[str, ...], str, str, ToolRiskLevel], ...] = (
    (("权限", "脱敏", "数据访问"), "security", "data_access", ToolRiskLevel.DANGEROUS),
    (("场景",), "system", "scenario", ToolRiskLevel.READ),
    (("取消", "作废"), "order", "cancel", ToolRiskLevel.WRITE),
    (
        ("确认订单", "更新订单状态", "标记已发货", "确认收货"),
        "order",
        "update_status",
        ToolRiskLevel.WRITE,
    ),
    (("修改订单", "改单", "变更订单"), "order", "modify", ToolRiskLevel.WRITE),
    (("查订单", "查询订单", "订单状态", "订单详情"), "order", "query", ToolRiskLevel.READ),
    (("下单", "创建订单", "新建订单", "购买", "采购"), "order", "create", ToolRiskLevel.WRITE),
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
    match = _QUANTITY_RE.search(query)
    if match is None:
        return None, None
    num_text = match.group(1)
    quantity: int | float = int(num_text) if "." not in num_text else float(num_text)
    return quantity, match.group(2)


def _extract_entities(query: str) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    for name in _PRODUCT_NAMES:
        if name in query:
            entities["product"] = name
            break
    for region in _REGIONS:
        if region in query:
            entities["region"] = region
            break
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
    entities = _extract_entities(masked)
    if order_id is not None:
        entities["order_id"] = order_id
    return IntentClassification(domain=domain, action=action, risk_level=risk, entities=entities)


def classify_intent_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node — classify the query and enter PLANNING."""
    return {"intent": classify_intent(state.query), "status": AgentStatus.PLANNING}
