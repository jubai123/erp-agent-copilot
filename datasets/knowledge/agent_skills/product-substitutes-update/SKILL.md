---
name: product-substitutes-update
description: 更新某商品的替代品（按替代品名称）。触发词：设置替代品、更新替代品、把某商品的替代品改成。
tool: updateProductSubstitutes
required_params: [product_id, substitute_name]
risk_level: write
required_scope: order:write
---
# 更新替代品
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- `product_id` 是被替代商品编号，`substitute_name` 是替代商品名称。
## Examples
- "将产品 ID 为 1 的替代品更新为产品 2 号" → {"product_id": 1, "substitute_name": "香蕉"}
