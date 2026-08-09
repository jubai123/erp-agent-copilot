---
document_id: api-product-stock
document_type: api_guide
business_domains: [product]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# API：查询商品库存

## 端点

```
GET /products/{product_id}/stock
```

## 功能

通过商品 ID 查询当前库存量。**该端点受场景切换影响**。

## 参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| product_id | path | integer | 是 | 商品 ID，1-6 |

## 返回值

**200 OK**：

```json
{
  "product_id": 1,
  "name": "苹果",
  "quantity_in_stock": 100,
  "unit": "KG"
}
```

## 场景影响

| 场景 | 行为 |
|------|------|
| happy_path | 返回种子数据中的实际库存（下单后会减少） |
| stock_insufficient | `quantity_in_stock` 强制返回 0 |
| supplier_unavailable | 不受影响，正常返回库存 |
| timeout | 不受影响，正常返回库存 |

## 错误码

| 状态码 | 说明 |
|--------|------|
| 404 | 商品 ID 不存在 |

## 调用约束

- 下单前必须调用此接口确认当前库存（而非依赖 GET /products/{name} 的 stock 值）。
- 在 stock_insufficient 场景下，即使种子数据中有库存，此接口也返回 0。
- 此接口返回的是查询时刻的快照，不保证查询后库存不变（虽然单用户场景下不会变）。

## 与其他接口的关系

- `GET /products/{name}` 返回完整商品信息（含库存），但库存量不受场景影响。
- 本接口仅返回库存相关字段，是下单前的必调接口。
- 下单 POST /orders 内部的库存校验与此接口共享同一库存数据源。
