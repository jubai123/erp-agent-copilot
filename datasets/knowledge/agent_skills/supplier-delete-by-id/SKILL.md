---
name: supplier-delete-by-id
description: 按供应商编号删除供应商。触发词：删除供应商、移除供应商，且给定供应商编号。
tool: deleteSupplierById
required_params: [supplier_id]
risk_level: write
required_scope: order:write
---
# 按编号删除供应商
## Instructions
- 写操作：风险 WRITE（M3 将升级为 DANGEROUS 强审批），需 `order:write` scope，执行前必须通过审批。
- `supplier_id` 是要删除的供应商编号。
- 删除是不可逆操作，执行前必须向用户展示待删除供应商摘要并确认。
## Examples
- "删除供应商 3 号" → {"supplier_id": 3}
