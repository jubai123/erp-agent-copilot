---
document_id: policy-idempotency
document_type: operation_policy
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

# 操作策略：幂等性

## 概述

幂等性确保同一操作多次执行产生相同结果，不会因网络重试、超时等原因产生重复数据。本项目通过 idempotency_key 机制实现下单幂等。

## 幂等键机制

### 工作原理

```
客户端生成 idempotency_key
        │
        ▼
POST /orders {..., idempotency_key: "abc-123"}
        │
        ▼
服务端查询 key 是否已存在
   │            │
  不存在        已存在
   │            │
   ▼            ▼
正常处理      返回缓存结果
返回 201      返回 200
```

### 幂等键要求

- **唯一性**：每次独立下单使用不同的 key。
- **一致性**：同一逻辑请求的重试使用相同的 key。
- **格式**：推荐 UUID v4（如 `550e8400-e29b-41d4-a716-446655440000`），也可使用业务唯一标识。
- **生成时机**：在用户确认下单后、调用 POST /orders 前生成。

## 使用场景

### 场景 1：正常下单

每次新下单生成新的 idempotency_key。不同 key → 不同订单。

### 场景 2：超时重试

POST /orders 返回 504 后，使用**相同 key** 重试：
- 若首次已处理 → 返回 200 + 缓存结果。
- 若首次未处理 → 正常处理，返回 201。

### 场景 3：网络错误重试

连接中断、DNS 解析失败等网络层错误，处理方式同超时：使用相同 key 重试。

## 幂等范围

- **范围**：per-client。不同客户端可使用相同 key 各自创建订单。
- **有效期**：幂等映射在服务端内存中维护，服务重启后失效。
- **不适用**：查询操作（GET）天然幂等，不需要幂等键。

## Agent 必须遵守

1. 每次新下单生成新的唯一 idempotency_key。
2. 重试时使用相同的 idempotency_key。
3. 不得复用之前的 idempotency_key 发起新订单。
4. 不得在未确认的情况下预先调用接口（"试探性"请求）。

## Agent 禁止执行

- 超时/失败后更换 idempotency_key 重试（导致重复订单）。
- 使用固定 key（如 "test-key"）发起多个不同订单。
- 超时后直接判定"下单失败"（忽略可能已成功的首次请求）。

## 关联文档

- [超时重试案例](../business-cases/case-timeout-retry.md)
- [订单模型](../domain-model/order-model.md)
- [L1 幂等性规则技能](../skills/skills.yaml#idempotency-rule)

## 验证标准

- 相同 key 两次 POST → 第二次返回 200（非 201），无重复订单。
- 不同 key 两次 POST → 各自返回 201，创建两个独立订单。
- 超时场景 + 相同 key 重试 → 不产生重复订单。
