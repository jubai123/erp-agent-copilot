---
name: product-batch-query
description: 按起始与终止商品编号批量查询商品信息（名称、库存、价格、描述）。触发词：批量查询、列出商品、商品 1 到 20 的基本信息。
tool: getBatchProductByProductIds
required_params: [start_id, end_id]
risk_level: read
required_scope: product:read
---
# 批量查询商品
## Instructions
- `start_id` / `end_id` 是商品编号区间（整数），返回该区间内的所有商品。
- 只读查询，不修改任何数据。
## Examples
- "列出产品 ID 从 1 到 20 的所有商品基本信息" → {"start_id": 1, "end_id": 20}
