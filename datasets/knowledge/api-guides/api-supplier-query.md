---
document_id: api-supplier-query
document_type: api_guide
business_domains: [supplier]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# API：查询供应商

## 端点

```
GET /suppliers
```

## 功能

按配送区域和/或状态筛选供应商列表。

## 参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| region | query | string | 否 | 配送区域，必须是 supplier_region_enum 中的值。不传则返回所有区域的供应商 |
| status | query | string | 否 | 供应商状态：AVAILABLE 或 UNAVAILABLE。不传则返回所有状态的供应商 |

## 返回值

**200 OK**：

```json
[
  {
    "supplier_id": 3,
    "name": "华东物流",
    "regions": ["上海", "南京"],
    "status": "AVAILABLE",
    "rating": 4.8,
    "delivery_days": 2,
    "price_per_kg": 1.2
  }
]
```

返回数组，可能为空数组（无匹配的供应商）。

## 场景影响

| 场景 | 行为 |
|------|------|
| happy_path | 返回种子数据中的实际状态 |
| supplier_unavailable | 所有供应商的 status 强制返回 UNAVAILABLE |
| stock_insufficient | 不受影响 |
| timeout | 不受影响 |

## 调用约束

- region 和 status 均为可选参数，不传则不做对应的过滤。
- region 必须精确匹配枚举值。
- 下单前必须调用此接口确认目标供应商可用（status=AVAILABLE）。

## 与其他接口的关系

- 下单 POST /orders 不自动校验供应商可用性，Agent 必须在下单前通过此接口验证。
- 在 supplier_unavailable 场景下，此接口返回空或全 UNAVAILABLE，Agent 应提示用户而非强行下单。
