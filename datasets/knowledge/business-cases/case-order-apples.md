---
document_id: case-order-apples
document_type: business_case
business_domains: [product, supplier, order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 案例：购买苹果

## 背景

用户在 happy_path 场景下，要从华东物流（supplier_id=3）采购 5 KG 苹果，配送到上海。

## 前置条件

- 系统处于 happy_path 场景。
- 苹果（product_id=1）库存充足（100 KG）。
- 华东物流（supplier_id=3）状态为 AVAILABLE，覆盖区域含上海。

## 操作步骤与预期

### 步骤 1：查询商品

```
GET /products/苹果
→ 200, {product_id: 1, name: "苹果", price: 10.0, unit: "KG"}
```

Agent 确认商品存在，记录 price=10.0、unit=KG。

### 步骤 2：查询库存

```
GET /products/1/stock
→ 200, {product_id: 1, quantity_in_stock: 100}
```

Agent 校验：5 ≤ 100，库存充足。

### 步骤 3：查询供应商

```
GET /suppliers?region=上海
→ 200, [{supplier_id: 3, name: "华东物流", status: "AVAILABLE", ...}]
```

Agent 筛选 status=AVAILABLE，确认华东物流覆盖上海。

### 步骤 4：展示订单摘要

Agent 向用户展示：

> 订单摘要：
> - 商品：苹果 × 5 KG
> - 单价：10.00 元/KG
> - 金额：50.00 元
> - 供应商：华东物流
> - 配送区域：上海
>
> 确认下单？

### 步骤 5：用户确认

用户确认后，Agent 生成 idempotency_key（如 `uuid-xxxx`），调用创建接口。

### 步骤 6：创建订单

```
POST /orders
{
  "product_id": 1,
  "quantity": 5,
  "supplier_id": 3,
  "region": "上海",
  "idempotency_key": "uuid-xxxx"
}
→ 201, {order_id: "a1b2c3d4e5f6", status: "CREATED", amount: 50.00, product_name: "苹果"}
```

### 步骤 7：验证结果

```
GET /orders/a1b2c3d4e5f6
→ 200, {status: "CREATED", amount: 50.00, product_name: "苹果"}
```

## 关键决策点

| 步骤 | 决策 | 依据 |
|------|------|------|
| 库存校验 | 5 ≤ 100，通过 | [stock-validation](../business-rules/stock-validation.md) |
| 供应商筛选 | 华东物流 AVAILABLE + 覆盖上海 | [supplier-selection](../business-rules/supplier-selection.md) |
| 金额计算 | 10.0 × 5 = 50.00 | 服务端计算，Agent 不自行计算 |
| 幂等键 | uuid-xxxx | 唯一标识本次请求 |

## 验证标准

- POST /orders 返回 201，amount=50.00。
- GET /orders/{order_id} 返回 status=CREATED。
- 库存从 100 扣减为 95。
