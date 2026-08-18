---
name: product-substitutes-lookup
description: 查询某商品的替代品，用于缺货比价或平替推荐。触发词：替代品、可替换、缺货买什么。
tool: getProductSubstitutesByName
required_params: [name]
risk_level: read
required_scope: product:read
---
# 查询替代品
## Instructions
- `name` 是商品名（非编号）。
- 返回替代品列表可能为空——空列表是合法业务答案，不代表失败。
- 只读查询，不修改任何数据。
## Examples
- "苹果的替代品" → {"name": "苹果"}
