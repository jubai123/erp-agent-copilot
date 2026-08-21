---
name: orders-by-status-query
description: 查询处于某状态的订单。触发词：运输中的订单、已发货的订单、按状态查订单。
tool: getByOrderStatus
required_params: [status]
risk_level: read
required_scope: order:read
---
# 按状态查询订单
## Instructions
- `status` 是订单状态枚举：CREATED / CONFIRMED / SHIPPED / DELIVERED / CANCELLED。
- 返回订单列表可能为空——空列表是合法业务答案。
- 只读查询，不修改任何数据。
## Examples
- "查询运输中的订单信息" → {"status": "SHIPPED"}
