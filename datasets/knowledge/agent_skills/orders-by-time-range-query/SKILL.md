---
name: orders-by-time-range-query
description: 按订单创建时间范围查询订单。触发词：某时间段/月份的订单、按日期范围查订单。
tool: getByTimeRange
required_params: [start_date, end_date]
risk_level: read
required_scope: order:read
---
# 按时间范围查询订单
## Instructions
- `start_date` / `end_date` 是订单创建时间范围的起止（ISO 格式，如 2023-01-01 / 2023-01-31）。
- 返回订单列表可能为空——空列表是合法业务答案。
- 只读查询，不修改任何数据。
## Examples
- "查询 2023 年 1 月 1 日至 1 月 31 日之间的订单" → {"start_date": "2023-01-01", "end_date": "2023-01-31"}
