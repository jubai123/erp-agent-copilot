---
document_id: api-order-create
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

# API：创建订单

## 端点

```
POST /orders
```

## 功能

创建新订单。包含库存校验、幂等去重和场景感知。**这是写操作，需要审批。**

## 请求体

```json
{
  "product_id": 1,
  "quantity": 5,
  "supplier_id": 3,
  "region": "上海",
  "idempotency_key": "client-gen-uuid-xxx"
}
```

| 参数 | 类型 | 必填 | 约束 |
|------|------|------|------|
| product_id | integer | 是 | 必须存在于商品目录中 |
| quantity | integer | 是 | 必须 > 0，且 ≤ 当前有效库存 |
| supplier_id | integer | 是 | 必须存在于供应商目录中 |
| region | string | 是 | 必须在 supplier_region_enum 中 |
| idempotency_key | string | 是 | 客户端生成的唯一标识 |

## 返回值

**201 Created**（首次请求）：

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

**200 OK**（重复幂等请求）：

返回首次请求的缓存结果，内容与 201 相同但状态码为 200。

## 错误码

| 状态码 | 场景 | 说明 |
|--------|------|------|
| 404 | product_id 不存在 | "Product with id {id} not found" |
| 422 | 库存不足 | "Insufficient stock: requested {qty} but only {stock} available" |
| 504 | timeout 场景 | "Order creation timeout" |

## 场景影响

| 场景 | 行为 |
|------|------|
| happy_path | 正常校验库存，成功创建并扣减库存 |
| stock_insufficient | 有效库存为 0，任何 quantity > 0 → 422 |
| supplier_unavailable | 不影响此接口（Agent 应在前置步骤验证供应商） |
| timeout | 返回 504，幂等重试可能也超时 |

## 调用约束

- **前置条件**：必须先查询库存（GET /products/{id}/stock）和供应商状态（GET /suppliers）。
- **幂等**：超时后使用相同 idempotency_key 重试，不重复创建。
- **审批**：创建订单是写操作，必须展示订单摘要并获取用户确认。
- **金额**：amount 由服务端计算（price × quantity），Agent 不得自行计算。
