---
document_id: rule-stock-validation
document_type: business_rule
business_domains: [product, order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 业务规则：库存校验

## 规则描述

下单前必须校验商品库存，库存不足时不得创建订单。

## 校验流程

1. 用户指定商品和数量。
2. Agent 调用 `GET /products/{product_id}/stock` 获取当前库存。
3. 比较 `quantity` 与 `quantity_in_stock`。
4. 若 `quantity <= quantity_in_stock`：允许继续下单。
5. 若 `quantity > quantity_in_stock`：必须拒绝下单，告知用户当前可用库存量。

## 场景影响

| 场景 | 有效库存 |
|------|----------|
| happy_path | 种子数据中的实际库存（下单后会扣减） |
| stock_insufficient | 强制为 0 |
| supplier_unavailable | 不受影响 |
| timeout | 不受影响 |

## 错误信息格式

```text
Insufficient stock: requested {quantity} but only {available} available
```

## 处置原则

- 库存不足时**不得**自动推荐替代商品（需等待用户决策）。
- 库存不足时**不得**创建数量为 0 或部分数量的订单。
- 库存为 0 时**不得**建议用户"试试下单"（明确告知不可下单）。

## 验证标准

- 正常库存：POST /orders 返回 201，quantity_in_stock 减少相应数量。
- 库存不足：POST /orders 返回 422，库存不减少。
- stock_insufficient 场景：任何 quantity > 0 → 422。
