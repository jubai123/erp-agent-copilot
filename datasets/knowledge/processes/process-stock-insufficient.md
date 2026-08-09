---
document_id: process-stock-insufficient
document_type: business_process
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

# 库存不足处理流程

## 适用条件

- 用户下单数量超过当前可用库存。
- 或系统处于 stock_insufficient 场景（所有库存为 0）。

## 处理步骤

1. **查询库存**：调用 `GET /products/{product_id}/stock`。
2. **发现库存不足**：quantity > quantity_in_stock（包括 stock_insufficient 场景下 quantity > 0）。
3. **告知用户**：准确报告当前可用库存量。例如："苹果当前库存为 0 KG，无法满足您订购 5 KG 的需求。"
4. **等待用户决策**：
   - 用户减少数量：重新从步骤 1 开始。
   - 用户更换商品：返回标准下单流程步骤 1。
   - 用户放弃：流程终止。

## 禁止行为

- **不得**自动减少下单数量（如用户要 5 个只有 3 个，不能自动改成 3 个）。
- **不得**自动推荐替代商品（如"苹果不够但香蕉够"）（除非用户明确要求）。
- **不得**创建数量大于库存的订单。
- **不得**跳过库存检查直接下单。

## 处置原则

- 库存不足是正常业务情况，不是系统错误。
- 错误信息应清晰准确：包含请求数量、可用数量、商品名称。
- stock_insufficient 场景下，即使种子数据显示有库存，Agent 也必须相信 API 返回的 0。

## 验证标准

- 库存不足 + 用户坚持下单 → Agent 拒绝创建订单，提示库存不足。
- 库存不足 + 用户减少数量到 ≤ 库存 → 正常下单成功。
- stock_insufficient 场景 → Agent 应识别所有商品库存为 0，无法下单。
