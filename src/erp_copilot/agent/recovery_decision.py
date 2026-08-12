"""Deterministic recovery action decision — real runner for the recovery eval.

Maps a natural-language query to one of four recovery actions (ask_missing /
confirm_conflict / retry / reject) with the same ordered, offline rules the
agent runtime reuses when a request cannot simply proceed (docs/06 §7):

1. retry       — the user asks to retry with the same idempotency key
                  (幂等/重试/再试/重放).
2. reject      — the request is illegal: cross-tenant access, a negative
                  quantity, a delivery region outside the enum, or re-cancelling
                  a terminal-state order.
3. conflict    — every checked parameter is present but a business rule is
                  violated: quantity above stock, unavailable supplier, illegal
                  status jump, cancellation fee, re-approval.
4. ask_missing — a required parameter (product/quantity/unit/region/order_id/
                  status) is absent; never invent it for the user.

Ordering notes: retry/reject win over everything (a missing parameter is never
the explanation for an illegal or retry request). Conflict is checked before
missing — a breach such as "下单 500 件键盘" (over keyboard stock 30) must
surface even though the query also omits a delivery region; each conflict check
is guarded by the parameters it needs, so a genuinely incomplete request still
falls through to ask_missing. This mirrors the agent's "surface the blocker
first, then collect what is missing" behaviour.

Local domain catalogs mirror the ERP Simulator seed data
(apps/erp_simulator/data) exactly as classify_intent mirrors
datasets/knowledge/manifest.yaml; mirror tests in
tests/unit/agent/test_recovery_decision.py guard against drift. The module does
not import the apps layer — src/ must not depend on process entry points.
"""

from __future__ import annotations

import re
from enum import StrEnum

from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.state import IntentClassification

# Local catalogs — mirror apps/erp_simulator/data seed (tests guard drift).
_REGIONS: frozenset[str] = frozenset(
    {
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
    }
)
_PRODUCT_STOCK: dict[str, int] = {
    "苹果": 100,
    "香蕉": 50,
    "橙子": 80,
    "电脑": 15,
    "键盘": 30,
    "鼠标": 50,
}
_REGION_SUPPLIER: dict[str, tuple[str, str]] = {
    "上海": ("华东物流", "AVAILABLE"),
    "南京": ("华东物流", "AVAILABLE"),
    "北京": ("华北物流", "AVAILABLE"),
    "天津": ("华北物流", "AVAILABLE"),
    "广州": ("华南物流", "UNAVAILABLE"),
    "深圳": ("华南物流", "UNAVAILABLE"),
    "成都": ("西南物流", "AVAILABLE"),
    "重庆": ("西南物流", "AVAILABLE"),
    "西安": ("西北物流", "AVAILABLE"),
    "兰州": ("西北物流", "AVAILABLE"),
}
# Legal order-state transitions (docs/06: CREATED→CONFIRMED→SHIPPED→DELIVERED).
_ORDER_STATE_GRAPH: dict[str, tuple[str, ...]] = {
    "CREATED": ("CONFIRMED",),
    "CONFIRMED": ("SHIPPED",),
    "SHIPPED": ("DELIVERED",),
    "DELIVERED": (),
}

_RETRY_TOKENS: tuple[str, ...] = ("幂等", "重试", "再试", "重放")
_CROSS_TENANT_TOKENS: tuple[str, ...] = ("跨租户", "另一个租户", "其他租户", "别的租户")
_TERMINAL_RECANCEL_TOKENS: tuple[str, ...] = ("已经取消", "已取消")
# classify_intent reads "-5" as quantity 5, so the minus sign is detected on the
# raw query text before entity extraction.
_NEGATIVE_QUANTITY_RE = re.compile(r"[-负]\s*\d+")
# A delivery verb + a 2-3 char region-like term suffixed with 地区/区域. Anchoring
# on the suffix keeps "给深圳下单" (region valid, no suffix) and "配送区域你定"
# (配送 is not a delivery verb) from being misread as an invalid region.
_REGION_TARGET_RE = re.compile(r"(?:给|到|发往|配送到|发到)\s*([一-鿿]{2,3})(?:地区|区域)")
_CURRENT_STATUS_RE = re.compile(r"从\s*(CREATED|CONFIRMED|SHIPPED|DELIVERED|已创建)")

# Required parameters checked in order; the first missing one wins.
_ACTION_REQUIRED_PARAMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("order", "create"): ("product", "quantity", "unit", "region"),
    ("order", "cancel"): ("order_id",),
    ("order", "query"): ("order_id",),
    ("order", "update_status"): ("order_id", "status"),
    ("order", "modify"): ("order_id", "new_quantity"),
    ("product", "check_stock"): ("product",),
}


class RecoveryAction(StrEnum):
    ASK_MISSING = "ask_missing"
    CONFIRM_CONFLICT = "confirm_conflict"
    RETRY = "retry"
    REJECT = "reject"


def _invalid_region(query: str) -> bool:
    """True when the query targets a delivery region outside the enum.

    A region is recognized only as a delivery verb (给/到/发往/配送到/发到)
    followed by a 2-3 char term suffixed with 地区/区域. "给华东地区" -> 华东
    (invalid, reject); valid-region deliveries carry no suffix, so "给深圳下单"
    and "配送区域你定" are not flagged.
    """
    match = _REGION_TARGET_RE.search(query)
    if match is None:
        return False
    return match.group(1) not in _REGIONS


def _has_conflict(intent: IntentClassification, domain: str, action: str, query: str) -> bool:
    """True when a business rule blocks the otherwise-complete request.

    Each branch is guarded by the parameters it needs, so an incomplete request
    (e.g. a cancel without an order id) falls through to ask_missing.
    """
    entities = intent.entities
    if (domain, action) == ("order", "create"):
        product = entities.get("product")
        quantity = entities.get("quantity")
        region = entities.get("region")
        if (
            isinstance(product, str)
            and product in _PRODUCT_STOCK
            and isinstance(quantity, (int, float))
            and quantity > _PRODUCT_STOCK[product]
        ):
            return True
        if region in _REGION_SUPPLIER and _REGION_SUPPLIER[region][1] == "UNAVAILABLE":
            return True
    elif (domain, action) == ("order", "update_status"):
        target = entities.get("status")
        match = _CURRENT_STATUS_RE.search(query)
        current = match.group(1) if match else None
        if (
            current in _ORDER_STATE_GRAPH
            and isinstance(target, str)
            and target not in _ORDER_STATE_GRAPH[current]
        ):
            return True
    elif (domain, action) == ("order", "cancel"):
        return "order_id" in entities
    elif (domain, action) == ("order", "modify"):
        return "order_id" in entities and "new_quantity" in entities
    return False


def _missing_param(domain: str, action: str, intent: IntentClassification) -> str | None:
    """Return the first absent required parameter for the intent, or None."""
    for param in _ACTION_REQUIRED_PARAMS.get((domain, action), ()):
        if param not in intent.entities:
            return param
    return None


def decide_recovery_action(query: str) -> RecoveryAction:
    """Deterministic recovery action for *query* (ordered procedure above)."""
    if any(token in query for token in _RETRY_TOKENS):
        return RecoveryAction.RETRY
    if any(token in query for token in _CROSS_TENANT_TOKENS):
        return RecoveryAction.REJECT
    if _NEGATIVE_QUANTITY_RE.search(query) is not None:
        return RecoveryAction.REJECT
    if _invalid_region(query):
        return RecoveryAction.REJECT
    if any(token in query for token in _TERMINAL_RECANCEL_TOKENS):
        return RecoveryAction.REJECT

    intent = classify_intent(query)
    domain, action = intent.domain, intent.action
    # classify_intent misses two business intents whose queries carry no tool
    # keyword: "订单…状态…改/更新" (update_status) and "订单…数量…改/变更"
    # (modify). The explicit patterns pin them to the right action.
    if "订单" in query and "状态" in query and ("改" in query or "更新" in query):
        domain, action = "order", "update_status"
    if "订单" in query and "数量" in query and ("改" in query or "变更" in query):
        domain, action = "order", "modify"

    if _has_conflict(intent, domain, action, query):
        return RecoveryAction.CONFIRM_CONFLICT
    if _missing_param(domain, action, intent) is not None:
        return RecoveryAction.ASK_MISSING
    return RecoveryAction.ASK_MISSING  # unreachable for the recovery dataset
