---
name: supplier-id-query
description: 按供应商编号查询供应商信息。触发词：供应商 N 号、供应商编号、supplier_id。
tool: getSupplierById
required_params: [supplier_id]
risk_level: read
required_scope: supplier:read
success_condition: "response.supplier_id == {supplier_id!r}"
---
# 按编号查询供应商
## Instructions
- `supplier_id` 是供应商整数主键，对应 manifest.yaml `suppliers` 的 `supplier_id` 字段。
- 只读查询，不修改任何数据。
## Examples
- "查询供应商 1 号的详细信息" → {"supplier_id": 1}
