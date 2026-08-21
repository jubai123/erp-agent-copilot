---
name: product-remove-by-id
description: 按商品编号删除商品。触发词：删除商品、下架商品、移除商品，且给定商品编号。
tool: removeProductById
required_params: [product_id]
risk_level: write
required_scope: order:write
---
# 按编号删除商品
## Instructions
- 写操作：风险 WRITE（M3 将升级为 DANGEROUS 强审批），需 `order:write` scope，执行前必须通过审批。
- `product_id` 是要删除的商品编号。
- 删除是不可逆操作，执行前必须向用户展示待删除商品摘要并确认。
## Examples
- "删除产品 ID 为 1 的产品" → {"product_id": 1}
