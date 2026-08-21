---
name: product-remove-by-name
description: 按名称删除商品。触发词：删除商品、下架商品、移除商品。写操作，必须走审批。
tool: removeProductByName
required_params: [name]
risk_level: write
required_scope: order:write
---
# 按名称删除商品
## Instructions
- 写操作：风险 WRITE（M3 将升级为 DANGEROUS 强审批），需 `order:write` scope，执行前必须通过审批。
- `name` 是要删除的商品名称。
- 删除是不可逆操作，执行前必须向用户展示待删除商品摘要并确认。
## Examples
- "夏季结束，西瓜已经下架，请从系统中删除西瓜" → {"name": "西瓜"}
