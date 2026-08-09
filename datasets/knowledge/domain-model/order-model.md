---
document_id: domain-order-model
document_type: domain_model
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

# 订单模型

## 概述

订单是 ERP 系统的核心交易实体，记录了用户下单的商品、数量、供应商、配送区域和金额。订单的创建、查询、状态变更和取消均通过 `/orders` 端点完成。

## 订单字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| order_id | string | 是 | 12 位十六进制字符串，服务端自动生成 |
| product_id | integer | 是 | 关联的商品 ID，必须存在于商品目录中 |
| product_name | string | 是 | 下单时的商品名称快照 |
| quantity | integer | 是 | 下单数量，必须 >0 且 ≤ 当前库存 |
| supplier_id | integer | 是 | 关联的供应商 ID，必须存在于供应商目录中 |
| region | string | 是 | 配送区域，必须在 supplier_region_enum 中 |
| amount | float | 是 | 订单金额 = 商品单价 × 数量，服务端计算，保留两位小数 |
| status | string | 是 | 当前状态，见下方状态机 |
| idempotency_key | string | 是 | 客户端生成的幂等键，用于去重 |
| created_at | string | 是 | ISO-8601 格式的 UTC 时间戳 |

## 订单状态机

```text
CREATED → CONFIRMED → SHIPPED → DELIVERED
CREATED → CANCELLED
CONFIRMED → CANCELLED
SHIPPED → CANCELLED
DELIVERED → (终态，不可变更)
CANCELLED → (终态，不可变更)
```

### 合法转换

| 当前状态 | 可转换到 |
|----------|----------|
| CREATED | CONFIRMED, CANCELLED |
| CONFIRMED | SHIPPED, CANCELLED |
| SHIPPED | DELIVERED, CANCELLED |
| DELIVERED | （无） |
| CANCELLED | （无） |

### 非法转换（必须拒绝）

- CANCELLED → 任何状态（已取消不可恢复）
- DELIVERED → 任何状态（已完成不可回退）
- 任何状态 → CREATED（不可回退到初始）
- 跳跃转换（如 CREATED → DELIVERED，跳过中间状态）

## 幂等机制

- 每次下单客户端生成唯一的 `idempotency_key`。
- 首次请求（key 不存在）：正常处理，返回 201。
- 重复请求（key 已存在）：返回缓存结果，HTTP 200，不创建新订单。
- 幂等范围：per-client，服务端维护 key→结果映射。
- 超时重试时使用相同的 idempotency_key 确保不重复下单。

## 库存校验

- 下单前必须检查 `quantity ≤ quantity_in_stock`。
- 库存不足返回 422，附带当前可用库存量的错误消息。
- `stock_insufficient` 场景下，有效库存强制为 0，任何正数量的下单请求都会被拒绝。

## 金额计算

- `amount = unit_price × quantity`，由服务端计算。
- Agent 不得自行计算或覆盖 amount 字段。
- 结果保留两位小数。
