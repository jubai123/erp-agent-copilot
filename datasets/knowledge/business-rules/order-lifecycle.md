---
document_id: rule-order-lifecycle
document_type: business_rule
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

# 业务规则：订单状态机

## 规则描述

订单的状态变更必须遵守预定义的状态机转换规则。非法状态转换必须被拦截。

## 状态定义

| 状态 | 含义 |
|------|------|
| CREATED | 订单已创建，等待确认 |
| CONFIRMED | 订单已确认，准备发货 |
| SHIPPED | 订单已发货，运输中 |
| DELIVERED | 订单已送达（终态） |
| CANCELLED | 订单已取消（终态） |

## 合法转换表

| 当前状态 → 目标状态 | 是否合法 | 说明 |
|---------------------|----------|------|
| CREATED → CONFIRMED | 合法 | 正常确认 |
| CREATED → CANCELLED | 合法 | 确认前取消 |
| CONFIRMED → SHIPPED | 合法 | 正常发货 |
| CONFIRMED → CANCELLED | 合法 | 发货前取消 |
| SHIPPED → DELIVERED | 合法 | 正常送达 |
| SHIPPED → CANCELLED | 合法 | 运输中取消（可能产生费用） |
| CANCELLED → 任何 | **非法** | 已取消不可恢复 |
| DELIVERED → 任何 | **非法** | 已送达不可回退 |
| 任何 → CREATED | **非法** | 不可回退到初始 |
| CREATED → SHIPPED | **非法** | 不可跳跃 CONFIRMED |
| CREATED → DELIVERED | **非法** | 不可跳跃 |

## 取消规则

- CREATED 和 CONFIRMED 状态的订单可无责取消。
- SHIPPED 状态可取消，但需告知用户可能产生物流拦截费用。
- DELIVERED 状态不可取消（已完成的走退货流程，不走取消）。
- 重复取消 CANCELLED 状态的订单应被拒绝。

## 审批要求

- CREATED → CONFIRMED：读操作，不需要审批。
- 任何 → CANCELLED：DANGEROUS 操作，需要审批。
- 状态变更操作必须验证订单当前状态。

## 验证标准

- 合法转换（CREATED → CONFIRMED）：操作成功。
- 非法转换（CANCELLED → CONFIRMED）：Agent 必须拦截，不发起调用。
- 跳跃转换（CREATED → DELIVERED）：Agent 必须拒绝。
