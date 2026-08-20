---
document_id: policy-approval-requirements
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

# 操作策略：审批要求

## 概述

所有写操作在用户确认或系统审批后方可执行。操作分为三个风险等级，对应不同的审批策略。

## 风险等级

| 等级 | 操作类型 | 示例 | 审批要求 |
|------|----------|------|----------|
| READ | 查询操作 | 查询商品、查询供应商、查询订单、查询库存 | 不需要审批 |
| WRITE | 创建操作 | POST /orders 创建订单 | 需要用户确认 |
| DANGEROUS | 破坏性操作 | 取消订单、修改订单状态 | 需要审批 + 合法性验证 |

## WRITE 操作：创建订单

### 确认流程

1. Agent 收集完商品、库存、供应商信息后，生成订单摘要。
2. 订单摘要必须包含：
   - 商品名称（product_name）
   - 数量（quantity）
   - 单价（price）
   - 总金额（amount = price × quantity）
   - 供应商名称（supplier_name）
   - 配送区域（region）
3. 展示给用户，等待明确确认。
4. 用户确认后，生成 idempotency_key，调用 POST /orders。

### 确认必须

- 用户明确表达"确认""下单""好的""可以"等肯定意图。
- Agent 不得根据上下文推断用户"可能同意"。

### 确认拒绝

- 用户表达"取消""不要""等等""再看看"等否定或犹豫意图。
- Agent 终止下单流程，不创建订单。

## DANGEROUS 操作：取消订单

### 审批流程

1. Agent 先查询订单当前状态（GET /orders/{order_id}）。
2. 判断可取消性：
   - CREATED/CONFIRMED → 可取消。
   - SHIPPED → 可取消但需特别提示。
   - DELIVERED → 不可取消。
   - CANCELLED → 不可重复取消。
3. 展示取消影响（产品、金额、可能产生的费用）。
4. 获取用户确认后执行。

### 审批必须

- 验证订单当前状态合法。
- 展示取消的完整影响（退款金额、可能费用）。
- 用户明确确认。

## 通用原则

- 所有审批确认必须是**显式**的，不得隐式推断。
- 审批失败或用户拒绝时，Agent 必须明确告知用户操作已取消。
- 不得在展示摘要的同时就发起操作（"番茄 5 KG 已下单" — 这是未确认就执行）。

## 关联文档

- [标准下单流程](../processes/process-order-creation.md)
- [订单取消流程](../processes/process-order-cancellation.md)
- [L1 审批策略规则](../rules/rules.yaml#approval-policy)

## 验证标准

- 创建订单前未展示摘要 → 违规。
- 取消订单前未验证状态 → 违规。
- 用户未确认即执行 → 违规。
