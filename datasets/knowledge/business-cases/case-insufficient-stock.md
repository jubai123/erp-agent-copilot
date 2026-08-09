---
document_id: case-insufficient-stock
document_type: business_case
business_domains: [product, order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 案例：库存不足

## 背景

用户要从华东物流采购 80 KG 香蕉，配送到上海。但香蕉当前库存仅 50 KG（或系统处于 stock_insufficient 场景，库存报告为 0）。

## 前置条件

- 系统可能处于 happy_path 或 stock_insufficient 场景。
- 香蕉（product_id=2）seed data 库存为 50 KG。

## 场景 A：happy_path 下库存不足

### 操作步骤

1. **查询商品**：`GET /products/香蕉` → 200，product_id=2，price=8.00。
2. **查询库存**：`GET /products/2/stock` → quantity_in_stock=50。
3. **库存校验**：80 > 50，库存不足。

### Agent 必须执行

- 告知用户："苹果当前库存为 50 KG，无法满足您订购 80 KG 的需求。"
- 等待用户决策：减少数量 / 更换商品 / 放弃。

### Agent 禁止执行

- 自动将数量改为 50。
- 自动推荐替代商品（如"香蕉不够但苹果够"）。
- 跳过库存检查直接创建订单。

### 用户减少数量后

若用户改为"香蕉 30 KG"：
- 重新查询库存，30 ≤ 50，校验通过。
- 继续正常下单流程。

## 场景 B：stock_insufficient 场景

系统处于 stock_insufficient 场景，所有商品库存报告为 0。

### 操作步骤

1. **查询库存**：`GET /products/2/stock` → quantity_in_stock=0。
2. **库存校验**：任何 quantity > 0 都导致库存不足。

### Agent 必须执行

- 告知用户："苹果当前库存为 0 KG，无法满足您订购 N KG 的需求。"
- 识别这是系统级库存不足（可能所有商品库存为 0）。

### 关键差异

| 场景 | 库存值 | Agent 行为 |
|------|--------|------------|
| happy_path | 50（真实库存） | 提示"库存 50，您的需求 80 无法满足" |
| stock_insufficient | 0（场景覆盖） | 提示"库存为 0，无法下单" |

## 关联文档

- [库存校验规则](../business-rules/stock-validation.md)
- [库存不足处理流程](../processes/process-stock-insufficient.md)

## 验证标准

- happy_path + 数量 > 库存 → Agent 拒绝下单，提示库存不足。
- 用户减少数量到 ≤ 库存 → 正常下单成功。
- stock_insufficient 场景 → Agent 拒绝所有下单请求。
