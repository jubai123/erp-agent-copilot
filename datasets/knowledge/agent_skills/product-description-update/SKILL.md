---
name: product-description-update
description: 更新某商品的描述信息。触发词：更新描述、改描述、修改商品信息。写操作，必须走审批。
tool: updateProductDescription
required_params: [product_id, description]
risk_level: write
required_scope: order:write
---
# 更新商品描述
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- `product_id` 是商品编号，`description` 是新的描述文本。
## Examples
- "将产品 ID 为 1 的商品描述更新为「有机苹果」" → {"product_id": 1, "description": "有机苹果"}
