---
name: product-substitutes-by-id
description: 按商品编号查询该商品的替代品，用于缺货比价或平替推荐。触发词：替代品、可替换、缺货买什么，且给定商品编号。
tool: getProductSubstitutes
required_params: [product_id]
risk_level: read
required_scope: product:read
---
# 按编号查询替代品
## Instructions
- `product_id` 是整数主键，对应 manifest.yaml `products` 的 `product_id` 字段。
- 返回替代品列表可能为空——空列表是合法业务答案，不代表失败。
- 只读查询，不修改任何数据。
## Examples
- "商品 4 号的替代品" → {"product_id": 4}
