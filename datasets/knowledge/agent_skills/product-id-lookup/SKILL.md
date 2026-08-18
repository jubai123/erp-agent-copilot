---
name: product-id-lookup
description: 按商品编号查询商品详情。用户给出数字编号而非名称时使用。触发词：商品 N 号、编号、product_id。
tool: getProductById
required_params: [product_id]
risk_level: read
required_scope: product:read
success_condition: "response.product_id == {product_id!r}"
---
# 按编号查询商品
## Instructions
- `product_id` 是整数主键（1–6），对应 manifest.yaml `products` 的 `product_id` 字段。
- 只读查询，不修改任何数据。
## Examples
- "商品 4 号的信息" → {"product_id": 4}
