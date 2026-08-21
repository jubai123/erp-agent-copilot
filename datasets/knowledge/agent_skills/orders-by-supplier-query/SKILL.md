---
name: orders-by-supplier-query
description: 查询某物流供应商配送的所有订单。触发词：某供应商的订单、按供应商查订单。
tool: getOrdersBySupplierId
required_params: [supplier_id]
risk_level: read
required_scope: order:read
---
# 按供应商查询订单
## Instructions
- `supplier_id` 是物流供应商编号（整数）。
- 返回订单列表可能为空——空列表是合法业务答案。
- 只读查询，不修改任何数据。
## Examples
- "查询由 id 为 3 的物流供应商配送的所有订单" → {"supplier_id": 3}
