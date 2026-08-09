---
document_id: security-data-access
document_type: security_policy
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

# 安全策略：数据访问控制

## 概述

ERP Agent Copilot 中的数据访问遵循最小权限原则和租户隔离原则。所有数据操作必须在授权的访问范围内进行。

## 访问范围

本项目定义以下访问范围：

| 范围标识 | 含义 | 适用操作 |
|----------|------|----------|
| knowledge:erp:read | 读取 ERP 知识库和业务数据 | 查询商品、供应商、订单、库存 |
| order:write | 创建和修改订单 | POST /orders、取消订单 |
| scenario:write | 切换系统场景 | PUT /scenario/{scenario_id} |

## 租户隔离

- 每个租户的数据在逻辑上隔离。
- Agent 操作只能在当前租户上下文中进行。
- 不得跨租户查询或修改数据。
- 幂等键的作用域为 per-client（即 per-tenant），不同租户的相同幂等键互不影响。

## 操作安全约束

### 读操作（READ）

- 允许：查询商品目录、查询库存、查询供应商、查询订单。
- 约束：仅返回当前租户可见的数据。
- 不需要审批。

### 写操作（WRITE）

- 允许：创建订单（POST /orders）。
- 约束：
  - 必须在用户确认后执行。
  - 必须校验库存和供应商可用性。
  - 必须提供合法的 idempotency_key。
- 需要用户确认。

### 危险操作（DANGEROUS）

- 允许：取消订单。
- 约束：
  - 必须验证订单状态合法（非终态）。
  - 必须展示取消影响。
  - 必须获取用户明确审批。
- 需要审批。

## 禁止行为

- **不得**在未验证权限的情况下执行任何写操作。
- **不得**绕过库存检查、供应商验证等业务约束。
- **不得**访问或修改其他租户的数据。
- **不得**在用户不知情的情况下切换系统场景。
- **不得**将订单数据、商品价格等业务信息泄露到对话上下文之外。

## 场景安全

- 场景切换（PUT /scenario）影响全局行为，仅用于测试和演示。
- 切换后应复位为 happy_path（通过 GET /scenario 确认当前场景）。
- 在生产环境中，场景控制端点应受额外保护。

## 数据最小化

- Agent 查询数据时只获取必要的字段。
- 不过度查询（如查询一个商品时不需要获取所有商品的库存）。
- 展示给用户的数据应精确匹配用户请求的上下文，不泄露无关信息。

## 关联文档

- [审批要求](../operation-policies/policy-approval-requirements.md)
- [幂等性策略](../operation-policies/policy-idempotency.md)
- [场景系统](../domain-model/scenario-system.md)

## 验证标准

- Agent 在写操作前展示确认信息 → 符合审批要求。
- Agent 拒绝在库存不足时下单 → 符合业务约束。
- Agent 拒绝取消 DELIVERED 订单 → 符合状态机约束。
