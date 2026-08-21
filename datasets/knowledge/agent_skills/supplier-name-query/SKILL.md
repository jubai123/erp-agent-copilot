---
name: supplier-name-query
description: 按供应商名称查询供应商信息。触发词：供应商 XX、查某供应商、京东/顺丰等供应商名字。
tool: getSupplierByName
required_params: [name]
risk_level: read
required_scope: supplier:read
---
# 按名称查询供应商
## Instructions
- `name` 是供应商名称（非编号）。
- 只读查询，不修改任何数据。
## Examples
- "查询京东的供应商信息" → {"name": "京东"}
