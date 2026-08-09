---
document_id: domain-scenario-system
document_type: domain_model
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

# 场景控制系统

## 概述

ERP 模拟器支持 4 种确定性场景，通过切换场景可以在不修改测试数据的情况下模拟不同的 ERP 故障模式。场景影响所有相关端点的行为，用于开发、测试和评测。

## 场景列表

| 场景名称 | 描述 | 影响的端点 |
|----------|------|-----------|
| happy_path | 所有端点正常行为，库存充足，供应商可用 | 全部端点 |
| stock_insufficient | 库存查询返回 0，下单被拒绝 | GET /products/{id}/stock, POST /orders |
| supplier_unavailable | 所有供应商返回 UNAVAILABLE | GET /suppliers |
| timeout | 订单创建返回 504 Gateway Timeout | POST /orders |

## 场景切换

- **查询当前场景**：`GET /scenario` → `{"scenario": "happy_path"}`
- **设置场景**：`PUT /scenario/{scenario_id}`，scenario_id 必须是上述 4 个值之一
- 场景切换立即生效，影响后续所有请求
- 测试后应复位为 `happy_path`

## 各场景详细行为

### happy_path

- 商品查询：返回种子数据中的实际库存
- 供应商查询：返回种子数据中的实际状态
- 订单创建：正常校验库存和供应商，成功后扣减库存

### stock_insufficient

- 库存查询：`quantity_in_stock` 强制返回 0（种子数据中的实际库存被忽略）
- 订单创建：任何 quantity > 0 的请求 → 422 "Insufficient stock"
- 其他端点行为不变

### supplier_unavailable

- 供应商查询：所有供应商的 status 强制返回 UNAVAILABLE
- 其他端点行为不变（但因为没有可用供应商，下单会失败）
- 用于测试 Agent 在无可用供应商时的提示行为

### timeout

- 订单创建：返回 504 Gateway Timeout
- 幂等机制仍然有效：相同 idempotency_key 的重试在 timeout 场景下可能也 timeout
- 用于测试 Agent 的超时重试和幂等保护行为

## 使用原则

1. 场景切换是全局的，所有并发请求共享同一场景状态。
2. 一次测试只使用一个场景，测试结束后复位。
3. 不要在 happy_path 下测试错误处理，不要在 stock_insufficient 下测试正常下单。
4. 场景不影响 GET /products/{name}（商品名称查询始终返回种子数据）。
