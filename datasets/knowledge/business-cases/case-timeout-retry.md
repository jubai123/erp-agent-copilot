---
document_id: case-timeout-retry
document_type: business_case
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

# 案例：超时重试与幂等性

## 背景

用户在 timeout 场景下下单。POST /orders 返回 504 Gateway Timeout。Agent 需要正确处理重试，确保不产生重复订单。

## 前置条件

- 系统处于 timeout 场景。
- 幂等键机制正常工作。

## 操作步骤

### 步骤 1：准备下单

Agent 已完成商品查询、库存校验、供应商筛选、用户确认，生成 idempotency_key=`abc-123`。

### 步骤 2：首次下单（超时）

```
POST /orders
{
  "product_id": 1,
  "quantity": 5,
  "supplier_id": 3,
  "region": "上海",
  "idempotency_key": "abc-123"
}
→ 504 Gateway Timeout
```

### 步骤 3：Agent 处理超时

Agent **不得**直接判定下单失败。正确的处理：

1. 告知用户："订单请求超时，正在使用相同的幂等键重试，不会产生重复订单。"
2. 使用**相同的 idempotency_key** 重新发起 POST /orders。
3. 若首次请求已成功处理（服务端在超时前完成了写入），幂等机制返回 200 + 缓存结果。
4. 若首次请求未被处理，服务端正常处理并返回 201。

### 步骤 4：重试结果处理

| 重试返回 | 含义 | Agent 行为 |
|----------|------|------------|
| 201 Created | 首次未被处理，本次创建成功 | 正常展示订单信息 |
| 200 OK（缓存结果） | 首次已处理，幂等返回 | 告知用户"订单已创建"，展示结果 |
| 再次 504 | 持续超时 | 告知用户"系统暂时不可用，请稍后重试" |

## Agent 必须遵守

1. 重试时**必须使用相同的 idempotency_key**，不得生成新 key。
2. 重试前**必须**告知用户正在重试及幂等保证。
3. 不得无限重试。建议最多重试 2 次（共 3 次尝试）。
4. 重试间隔建议 1-2 秒。

## Agent 禁止执行

- 超时后生成新的 idempotency_key 重试（会产生重复订单）。
- 超时后直接告知用户"下单失败"（可能已成功）。
- 不做任何提示直接静默重试。

## 关联文档

- [幂等性规则](../business-rules/order-lifecycle.md)（idempotency_key 定义）
- [幂等性操作策略](../operation-policies/policy-idempotency.md)

## 验证标准

- timeout 场景 + 相同 key 重试 → 不产生重复订单。
- Agent 在重试前告知用户幂等保证。
- 3 次尝试均超时 → Agent 终止并告知用户稍后重试。
