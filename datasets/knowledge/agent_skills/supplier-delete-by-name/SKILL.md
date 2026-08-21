---
name: supplier-delete-by-name
description: 按名称删除供应商。触发词：删除供应商、移除供应商。写操作，必须走审批。
tool: deleteSupplierByName
required_params: [name]
risk_level: dangerous
required_scope: order:write
---
# 按名称删除供应商
## Instructions
- 写操作：风险 DANGEROUS（强审批），需 `order:write` scope，执行前必须通过审批。
- `name` 是要删除的供应商名称。
- 删除是不可逆操作，执行前必须向用户展示待删除供应商摘要并确认。
## Examples
- "删除供应商「旧物流」" → {"name": "旧物流"}
