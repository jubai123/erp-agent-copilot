---
name: orders-by-product-query
description: 查询某商品的所有订单。触发词：某商品的订单、按商品查订单。
tool: getByProductId
required_params: [product_id]
risk_level: read
required_scope: order:read
---
# 按商品查询订单
## Instructions
- `product_id` 是商品编号（整数）。
- 返回订单列表可能为空——空列表是合法业务答案。
- 只读查询，不修改任何数据。
## Examples
- "查询产品 ID 为 1 的订单信息" → {"product_id": 1}
