"""Deterministic intent classifier node — task 4.3.

Maps a natural-language query to a structured (domain, action) intent with an
initial risk level and explicit entities (docs/03 §4).  Deterministic by the
ADR rule "确定性优先，概率兜底": the (domain, action) output feeds two
downstream deterministic layers — rule_matcher (L1 rule injection) and
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

import calendar
import re
from typing import Any

from erp_copilot.agent.state import AgentState, AgentStatus, IntentClassification
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.vocabulary.loader import get_catalog, get_quantity_pattern

# order_id is a lowercase hex string (manifest order_fields says 12 chars; the
# planning dataset uses shorter ids like 3f2a9c1d / a1b2c3 / 20260809001).
_ORDER_ID_RE = re.compile(r"[0-9a-f]{6,12}")
# product_id: "商品 4 号" / "产品 5 号" / "编号 4" / "产品 ID 为 1" / "产品 ID: 1".
_PRODUCT_ID_RE = re.compile(
    r"商品\s*(\d+)\s*号|产品\s*(\d+)\s*号|编号\s*(\d+)|产品\s*ID\s*[:：为]?\s*(\d+)"
)
# supplier_id: "供应商 3 号" / "id 为 3 的物流供应商" (the id token must lead to
# a 供应商 mention, so "产品 ID 为 3" is not misread as a supplier id).
_SUPPLIER_ID_RE = re.compile(r"供应商\s*(\d+)\s*号|(?:id|ID)\s*[:：为]?\s*(\d+).*?供应商")
# supplier name: "供应商「旧物流」" / "供应商 旧物流" (stops at 的/连接词/
# 覆盖性动词/状态类动词/标点/句尾 so "供应商的订单" / "供应商 旧物流 覆盖上海"
# / "供应商挑可用的" do not over-capture the trailing clause).
_SUPPLIER_NAME_RE = re.compile(
    r"供应商\s*[「『\"']?([^的是有哪些哪家\n，。、和与及为覆盖包括挑选用要可找]{2,}?)"
    r"(?:[」』\"']|的|，|。|$)"
)
# product/supplier name after "名称为" for create tools ("添加名称为西瓜的商品").
_NAME_AFTER_RE = re.compile(r"名称为\s*([^的，。、\s]{2,})")
_PRICE_RE = re.compile(r"(?:价格|单价)\s*[:：为]?\s*(\d+(?:\.\d+)?)")
_STOCK_RE = re.compile(r"库存\s*[:：为]?\s*(\d+)")
_DESCRIPTION_RE = re.compile(r"(?:更新|改|修改)?描述(?:为|到)?\s*[「『\"']?([^」』\"'，。]{1,80})")
_SUBSTITUTE_NAME_RE = re.compile(r"替代品.*?(?:为|改成|改为)\s*([一-龥A-Za-z0-9]{2,})")
# New quantity in a modify query ("数量从 10 改成 20" -> 20). Distinct from the
# loader-built quantity pattern, which keeps the *first* number as the original
# quantity.
_NEW_QUANTITY_RE = re.compile(r"(?:改成|改为)\s*(\d+)")
# Date range for getByTimeRange ("2023年1月1日"/"2023年1月"/"2023-01-01"); order
# ids are blanked before this runs so a hex id cannot fake a date.
_DATE_RANGE_RE = re.compile(r"\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{4}-\d{1,2}-\d{1,2}")

# Order-status verbs -> ERP status string (ordered: English tokens win, then
# the Chinese verb with the more specific meaning first).
_STATUS_MAP: tuple[tuple[str, str], ...] = (
    ("SHIPPED", "SHIPPED"),
    ("DELIVERED", "DELIVERED"),
    ("CONFIRMED", "CONFIRMED"),
    ("CANCELLED", "CANCELLED"),
    ("已发货", "SHIPPED"),
    ("运输中", "SHIPPED"),
    ("确认收货", "DELIVERED"),
    ("已送达", "DELIVERED"),
    ("确认订单", "CONFIRMED"),
    ("待确认", "CONFIRMED"),
    ("已取消", "CANCELLED"),
)

# Intent rules in priority order — first match wins.  Keys stay in sync with
# datasets/knowledge/rules/intent_rule_map.yaml and candidate_filter.
# Order query sub-intents must sit above order/cancel and order/update_status so
# "已发货的订单"/"已取消的订单" route to the read, not the write; supplier
# maintenance must sit above supplier/query ("供应商" would swallow it).
_INTENT_RULES: tuple[tuple[tuple[str, ...], str, str, ToolRiskLevel], ...] = (
    (("权限", "脱敏", "数据访问"), "security", "data_access", ToolRiskLevel.DANGEROUS),
    (("场景",), "system", "scenario", ToolRiskLevel.READ),
    (
        (
            "按状态查询订单",
            "运输中的订单",
            "已发货的订单",
            "待确认的订单",
            "已送达的订单",
            "已取消的订单",
        ),
        "order",
        "query_by_status",
        ToolRiskLevel.READ,
    ),
    (
        ("时间段", "时间范围", "按时间查询订单", "本月的订单"),
        "order",
        "query_by_time",
        ToolRiskLevel.READ,
    ),
    (
        ("的订单信息", "该商品的订单", "按商品查询订单", "产品的订单"),
        "order",
        "query_by_product",
        ToolRiskLevel.READ,
    ),
    (
        ("配送的所有订单", "供应商的订单", "按供应商查询订单"),
        "order",
        "query_by_supplier",
        ToolRiskLevel.READ,
    ),
    (("取消", "作废"), "order", "cancel", ToolRiskLevel.WRITE),
    # Product write intents sit ABOVE order/update_status: its bare verb
    # keywords ("改为", "更新为") would otherwise swallow product maintenance
    # ("把商品 4 号的替代品改为西瓜" is a product/update, not an order status
    # change).  The order query_by_* rules above still win for order reads.
    (
        ("添加商品", "新增商品", "上架商品", "添加名称为", "新增名称为"),
        "product",
        "add",
        ToolRiskLevel.WRITE,
    ),
    (
        ("修改商品", "更新商品", "改商品", "更新描述", "修改描述", "替代品改为", "替代品改成"),
        "product",
        "update",
        ToolRiskLevel.WRITE,
    ),
    (
        ("删除商品", "删除产品", "下架商品", "移除商品"),
        "product",
        "delete",
        ToolRiskLevel.DANGEROUS,
    ),
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
    (("添加供应商", "新增供应商", "注册供应商"), "supplier", "create", ToolRiskLevel.WRITE),
    (("删除供应商", "移除供应商"), "supplier", "delete", ToolRiskLevel.DANGEROUS),
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
    # A concrete date range alongside 订单 that matched no keyword rule is a
    # time-range order query ("查询2023年1月的订单" has no keyword of its own).
    if _DATE_RANGE_RE.search(query) and "订单" in query:
        return ("order", "query_by_time", ToolRiskLevel.READ)
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
    for group in match.groups():
        if group is not None:
            return int(group)
    return None


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


def _extract_supplier_id(query: str) -> int | None:
    match = _SUPPLIER_ID_RE.search(query)
    if match is None:
        return None
    return int(match.group(1) if match.group(1) is not None else match.group(2))


def _extract_supplier_name(query: str) -> str | None:
    match = _SUPPLIER_NAME_RE.search(query)
    return match.group(1) if match else None


def _extract_name_after(query: str) -> str | None:
    match = _NAME_AFTER_RE.search(query)
    return match.group(1) if match else None


def _extract_price(query: str) -> float | None:
    match = _PRICE_RE.search(query)
    return float(match.group(1)) if match else None


def _extract_stock(query: str) -> int | None:
    match = _STOCK_RE.search(query)
    return int(match.group(1)) if match else None


def _extract_description(query: str) -> str | None:
    match = _DESCRIPTION_RE.search(query)
    return match.group(1) if match else None


def _extract_substitute_name(query: str) -> str | None:
    match = _SUBSTITUTE_NAME_RE.search(query)
    return match.group(1) if match else None


def _normalize_date(text: str) -> str:
    """Normalize "2023年1月1日" / "2023-01-01" / "2023年1月" to YYYY-MM-DD."""
    full = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", text)
    if full:
        y, m, d = (int(full.group(i)) for i in (1, 2, 3))
        return f"{y:04d}-{m:02d}-{d:02d}"
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        y, m, d = (int(iso.group(i)) for i in (1, 2, 3))
        return f"{y:04d}-{m:02d}-{d:02d}"
    month = re.fullmatch(r"(\d{4})年(\d{1,2})月", text)
    if month:
        y, m = int(month.group(1)), int(month.group(2))
        return f"{y:04d}-{m:02d}-01"
    return text


def _extract_time_range(query: str) -> tuple[str, str] | None:
    """Extract (start, end) dates from a date-range mention, or None.

    "2023年1月1日至2023年1月31日" yields both bounds; a lone month mention
    ("2023年1月") expands to the whole month; a lone day is a zero-width range.
    """
    matches = list(_DATE_RANGE_RE.finditer(query))
    if not matches:
        return None
    texts = [m.group(0) for m in matches]
    if len(texts) >= 2:
        return (_normalize_date(texts[0]), _normalize_date(texts[1]))
    start = _normalize_date(texts[0])
    month = re.fullmatch(r"(\d{4})年(\d{1,2})月", texts[0])
    if month:
        y, m = int(month.group(1)), int(month.group(2))
        return (start, f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}")
    return (start, start)


def _extract_entities(query: str) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    catalog = get_catalog()
    for name in catalog.products:
        if name in query:
            entities["product"] = name
            break
    regions = [region for region in catalog.regions if region in query]
    if regions:
        entities["region"] = regions[0]
        entities["regions"] = regions
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
    supplier_id = _extract_supplier_id(query)
    if supplier_id is not None:
        entities["supplier_id"] = supplier_id
    supplier_name = _extract_supplier_name(query)
    if supplier_name is not None:
        entities["supplier_name"] = supplier_name
    name_after = _extract_name_after(query)
    if name_after is not None:
        entities["name"] = name_after
    price = _extract_price(query)
    if price is not None:
        entities["price"] = price
    stock = _extract_stock(query)
    if stock is not None:
        entities["stock"] = stock
    description = _extract_description(query)
    if description is not None:
        entities["description"] = description
    substitute_name = _extract_substitute_name(query)
    if substitute_name is not None:
        entities["substitute_name"] = substitute_name
    time_range = _extract_time_range(query)
    if time_range is not None:
        entities["time_range"] = time_range
    # Bare digits already claimed by an id or date must not leak as quantity
    # ("查商品 4 号" -> product_id 4, not quantity 4; "2023年1月" -> the year is
    # a date, not a quantity; "库存为 500" -> quantity_in_stock, not a purchase
    # quantity).
    _digits_claimed = any(
        k in entities for k in ("product_id", "supplier_id", "time_range")
    )
    if not _digits_claimed and "库存" not in query:
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
    # A query that matched no rule but names a cross-domain entity is order
    # domain: a concrete order id ("订单 7c9e1d5 的金额", plan-018) is an order
    # lookup; 商品/产品/供应商 + 订单 without an order id ("商品 3 号的订单")
    # is the corresponding cross-entity order query. Without these, such
    # queries would fall to the product fallback and be misread.
    if (domain, action) == ("product", "query"):
        if order_id is not None:
            domain, action, risk = "order", "query", ToolRiskLevel.READ
        elif "订单" in masked and "供应商" in masked:
            domain, action, risk = "order", "query_by_supplier", ToolRiskLevel.READ
        elif "订单" in masked and ("商品" in masked or "产品" in masked):
            domain, action, risk = "order", "query_by_product", ToolRiskLevel.READ
    elif (domain, action) == ("supplier", "query") and "订单" in masked:
        # "供应商 3 号的订单" matched the bare 供应商 keyword; the 订单 marker
        # upgrades it to the cross-entity order query.
        domain, action, risk = "order", "query_by_supplier", ToolRiskLevel.READ
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
