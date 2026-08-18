---
name: order-cancel
description: 取消订单。订单进入终态（DELIVERED / CANCELLED）之前才可取消。触发词：取消订单、退单、不买了。写操作，必须走审批。
tool: cancelOrder
required_params: [order_id]
risk_level: write
required_scope: order:write
---
# 取消订单
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- 取消前应先用 order-lookup 确认订单非终态。
- 补偿语义：取消失败时按 fallback 重激活原订单（补偿对账）。
## Examples
- "取消订单 3f2a9c1d" → {"order_id": "3f2a9c1d"}
