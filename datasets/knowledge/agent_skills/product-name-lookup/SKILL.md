---
name: product-name-lookup
description: 按商品名称查询商品详情（价格、库存、单位）。用户直接说出商品名且需要商品信息时使用。触发词：多少钱、价格、库存、商品信息。
tool: getProductByName
required_params: [name]
risk_level: read
required_scope: product:read
success_condition: "response.name == {name!r}"
---
# 按名称查询商品
## Instructions
- `name` 是商品名（非编号），须在 manifest.yaml `products` 列表内：苹果 / 香蕉 / 橙子 / 电脑 / 键盘 / 鼠标。
- 返回字段含 `price`、`quantity_in_stock`、`unit`；`unit` 枚举为 KG / 台 / 件。
- 只读查询，不修改任何数据。
## Examples
- "苹果多少钱" → {"name": "苹果"}
