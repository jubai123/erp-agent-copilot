---
name: order-lookup
description: 按订单号查询订单详情（状态、金额、商品）。用户给出订单号并想查状态或详情时使用。触发词：查订单、订单状态、订单号。
tool: getOrderByOrderId
required_params: [order_id]
risk_level: read
required_scope: order:read
success_condition: "response.order_id == {order_id!r}"
---
# 按订单号查询订单
## Instructions
- `order_id` 是 12 位十六进制字符串，由服务端生成（manifest.yaml `order_fields`）。
- 只读查询，不修改任何数据。
## Examples
- "查订单 3f2a9c1d" → {"order_id": "3f2a9c1d"}
