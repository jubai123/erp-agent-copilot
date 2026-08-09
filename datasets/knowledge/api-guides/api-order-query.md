---
document_id: api-order-query
document_type: api_guide
business_domains: [order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# API：查询订单

## 端点

```
GET /orders/{order_id}
```

## 功能

通过订单 ID 查询订单详细信息（状态、金额、产品、供应商等）。

## 参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| order_id | path | string | 是 | 订单 ID，12 位十六进制字符串 |

## 返回值

**200 OK**：

```json
{
  "order_id": "a1b2c3d4e5f6",
  "product_id": 1,
  "product_name": "苹果",
  "quantity": 5,
  "supplier_id": 3,
  "region": "上海",
  "amount": 50.0,
  "status": "CREATED",
  "idempotency_key": "client-gen-uuid-xxx",
  "created_at": "2026-08-06T12:00:00+00:00"
}
```

## 错误码

| 状态码 | 说明 |
|--------|------|
| 404 | 订单 ID 不存在 |

## 调用约束

- 创建订单后，建议通过此接口验证订单创建成功（状态、金额、产品一致）。
- 在验证阶段（verify_results）中，调用此接口比对返回值与期望值。
- 该接口是只读操作，不需要审批，不受场景切换影响。

## 与其他接口的关系

- 创建订单后调用：验证 `order_id` 对应的订单 `status == "CREATED"` 且 product/quantity/amount 一致。
- 取消订单前调用：确认订单当前状态可取消（非 DELIVERED/CANCELLED）。
