---
document_id: process-order-creation
document_type: business_process
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

# 标准下单流程

## 适用条件

用户明确表达下单意图，并至少提供产品名称、数量和配送区域。

## 前置条件

- 系统处于 happy_path 场景。
- 用户具有 order:write 权限。

## 处理步骤

1. **查询商品**：调用 `GET /products/{name}` 确认商品存在，获取 price 和 unit。
2. **查询库存**：调用 `GET /products/{product_id}/stock` 获取当前库存。
3. **校验库存**：比较 quantity 与 quantity_in_stock。不足 → 告知用户，流程终止。
4. **查询供应商**：调用 `GET /suppliers?region={region}` 筛选该区域的供应商。
5. **筛选可用供应商**：过滤 status=AVAILABLE，按评分、时效、价格排序。
6. **展示订单摘要**：向用户展示 product_name × quantity = amount（price × quantity），supplier，region。
7. **获取用户确认**：等待用户确认下单。用户拒绝 → 流程终止。
8. **生成幂等键**：为本次下单生成唯一 idempotency_key。
9. **创建订单**：调用 `POST /orders`，传递 product_id、quantity、supplier_id、region、idempotency_key。
10. **验证结果**：调用 `GET /orders/{order_id}` 确认 status=CREATED，amount 和 product_name 一致。

## 处置原则

- 任何步骤失败时不得跳过，必须中断并告知用户失败原因。
- 步骤 1-6 为 READ 操作，可并行执行（其中 1-2 有依赖，3 依赖 2）。
- 步骤 9 为 WRITE 操作，必须在步骤 7（用户确认）之后执行。
- 库存不足时不得创建订单。
- 供应商不可用时不得编造 supplier_id。

## 验证标准

- 订单创建成功：POST /orders 返回 201，GET /orders/{order_id} 返回 status=CREATED。
- 金额正确：amount = price × quantity，保留两位小数。
- 库存扣减：下单前和下订单后，quantity_in_stock 减少相应的数量。
