---
name: order-create
description: 创建新订单（下单）。需先查商品与供应商；仅当商品库存充足且选定区域可配送时使用。触发词：下一单、下单、帮我买、订购。写操作，必须走审批。
tool: createOrder
required_params: [product_id, supplier_id, quantity, region]
risk_level: write
required_scope: order:write
success_condition: "response.amount > 0"
---
# 创建订单
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- `quantity` 必须 > 0 且 ≤ 商品 `quantity_in_stock`（manifest.yaml `stock_validation`）。
- `region` 必须在 `supplier_region_enum` 内，且选定供应商在该区域可配送。
- 幂等：请求须带 `idempotency_key`；同 key 重复提交返回缓存结果（at-most-once）。
## Examples
- "帮我在上海下一单 10 KG 苹果" → {"quantity": 10, "region": "上海"}
