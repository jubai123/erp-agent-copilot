---
name: product-create
description: 添加新商品到目录。触发词：添加商品、新增商品、上架商品。写操作，必须走审批。
tool: addProduct
required_params: [name, price, quantity_in_stock]
risk_level: write
required_scope: order:write
---
# 添加商品
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- `name` 是商品名（枚举：苹果、香蕉、橙子、电脑、键盘、鼠标）。
- `price` 为单价（元），`quantity_in_stock` 为初始库存。
- `unit` 须为 product_unit_enum 中的枚举值。
## Examples
- "添加名称为西瓜、价格 5 元、库存 100 的商品" → {"name": "西瓜", "price": 5, "quantity_in_stock": 100}
