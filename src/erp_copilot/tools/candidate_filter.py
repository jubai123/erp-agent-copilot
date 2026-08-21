"""Deterministic tool candidate filtering — ADR decision 5.

First level: (domain, action) → 3-8 candidate tools via DOMAIN_TOOL_MAP,
shrinking the build_plan LLM's choice space from 25 to 3-5 tools.
Second level (reserved, not implemented): vector recall + rerank, enabled
only when candidates exceed the threshold.

The intent keys MUST match intent_rule_map.yaml — L1 rule injection and
tool filtering share one intent taxonomy.

V5's 25-tool two-stage design (vector recall + rerank as the only engine)
is preserved here as the *on-demand* growth path: when a domain grows past
the threshold, filter_candidates still returns the deterministic subset,
and should_use_tool_retrieval flags that a rerank pass is warranted.
"""

from __future__ import annotations

# V6 registered tool set — the full V5 cloud-ERP surface (25 tools), so every
# OpenAPI interface is inside the decision scope (docs/05; the ADR's "25 选 1
# 缩小到 3 选 1" shrink is re-derived from this set). Write/delete tools are
# admitted here before their executors exist (M1-M3) so NL routing and planning
# are aligned up front; execution lands per-milestone.
V6_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # products — read
        "getProductByName",
        "getProductById",
        "getProductSubstitutesByName",
        "getProductSubstitutes",
        "getBatchProductByProductIds",
        # products — write / delete
        "addProduct",
        "updateProductDescription",
        "updateProductSubstitutes",
        "removeProductByName",
        "removeProductById",
        # suppliers — read
        "querySuppliersByDeliveryRegion",
        "getSupplierByStatus",
        "getSupplierByName",
        "getSupplierById",
        # suppliers — write / delete
        "addSuppliers",
        "deleteSupplierByName",
        "deleteSupplierById",
        # orders — read
        "getOrderByOrderId",
        "getOrdersBySupplierId",
        "getByTimeRange",
        "getByProductId",
        "getByOrderStatus",
        # orders — write
        "createOrder",
        "updateOrderStatus",
        "cancelOrder",
    }
)

# Intent keys mirror datasets/knowledge/rules/intent_rule_map.yaml.
# Every intent keeps at most TOOL_RETRIEVAL_THRESHOLD candidates so the
# second-level rerank stays off (each domain's read tools fit in 5).
DOMAIN_TOOL_MAP: dict[tuple[str, str], list[str]] = {
    ("product", "query"): [
        "getProductByName",
        "getProductById",
        "getProductSubstitutesByName",
        "getProductSubstitutes",
        "getBatchProductByProductIds",
    ],
    ("product", "check_stock"): [
        "getProductByName",
        "getProductById",
        "getProductSubstitutesByName",
        "getProductSubstitutes",
    ],
    ("product", "add"): [
        "addProduct",
        "getProductByName",
    ],
    ("product", "update"): [
        "updateProductDescription",
        "updateProductSubstitutes",
        "getProductByName",
        "getProductById",
    ],
    ("product", "delete"): [
        "removeProductByName",
        "removeProductById",
        "getProductByName",
        "getProductById",
    ],
    ("supplier", "query"): [
        "querySuppliersByDeliveryRegion",
        "getSupplierByStatus",
        "getSupplierByName",
        "getSupplierById",
    ],
    ("supplier", "create"): [
        "addSuppliers",
        "getSupplierByName",
        "getSupplierByStatus",
    ],
    ("supplier", "delete"): [
        "deleteSupplierByName",
        "deleteSupplierById",
        "getSupplierByName",
        "getSupplierByStatus",
    ],
    ("order", "create"): [
        "getProductByName",
        "querySuppliersByDeliveryRegion",
        "getSupplierByStatus",
        "createOrder",
    ],
    ("order", "query"): [
        "getOrderByOrderId",
        "getOrdersBySupplierId",
        "getByTimeRange",
        "getByProductId",
        "getByOrderStatus",
    ],
    ("order", "query_by_time"): [
        "getByTimeRange",
        "getOrderByOrderId",
    ],
    ("order", "query_by_status"): [
        "getByOrderStatus",
        "getOrderByOrderId",
    ],
    ("order", "query_by_product"): [
        "getByProductId",
        "getOrderByOrderId",
    ],
    ("order", "query_by_supplier"): [
        "getOrdersBySupplierId",
        "getOrderByOrderId",
    ],
    ("order", "cancel"): [
        "getOrderByOrderId",
        "cancelOrder",
        "createOrder",
    ],
    ("order", "update_status"): [
        "getOrderByOrderId",
        "updateOrderStatus",
    ],
    ("order", "modify"): [
        "getOrderByOrderId",
        "cancelOrder",
        "createOrder",
    ],
    ("security", "data_access"): [],
    ("system", "scenario"): [],
}

# Candidates strictly above this count trigger the second-level rerank.
TOOL_RETRIEVAL_THRESHOLD = 5


def filter_candidates(
    domain: str,
    action: str,
    available_tools: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Return the deterministic candidate tools for an intent.

    Intersects DOMAIN_TOOL_MAP with the registered tool set (defaults to
    V6_TOOL_NAMES), preserving the mapping order.  Unknown intents return
    an empty list — never an error.
    """
    mapped = DOMAIN_TOOL_MAP.get((domain, action), [])
    if not mapped:
        return []
    available = V6_TOOL_NAMES if available_tools is None else frozenset(available_tools)
    return [tool for tool in mapped if tool in available]


def should_use_tool_retrieval(
    candidates: list[str],
    threshold: int = TOOL_RETRIEVAL_THRESHOLD,
) -> bool:
    """Decide whether the second-level (vector + rerank) stage is needed.

    Enabled only when candidates strictly exceed the threshold — with 25 tools
    no intent reaches it (every domain's candidates fit in 5), so the rerank
    stage stays off.
    """
    return len(candidates) > threshold
