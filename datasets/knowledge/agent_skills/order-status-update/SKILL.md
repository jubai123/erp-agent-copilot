---
name: order-status-update
description: 更新订单状态。状态流转按 order_status_lifecycle：CREATED→CONFIRMED→SHIPPED→DELIVERED，任一步可 CANCELLED。触发词：改状态、改为已发货、确认订单。写操作，必须走审批。
tool: updateOrderStatus
required_params: [order_id, status]
risk_level: write
required_scope: order:write
---
# 更新订单状态
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- 目标状态须在 order_status_lifecycle 中合法：CREATED / CONFIRMED / SHIPPED / DELIVERED / CANCELLED。
- 终态 DELIVERED / CANCELLED 不可再流转。
## Examples
- "把订单 a1b2c3 改为已发货" → {"order_id": "a1b2c3", "status": "SHIPPED"}
