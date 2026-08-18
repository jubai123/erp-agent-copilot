---
name: supplier-by-status-query
description: 按状态查询供应商。用户未指定区域、只问可用/在供供应商时使用。触发词：可用的供应商、AVAILABLE、有哪些供应商在供货。
tool: getSupplierByStatus
required_params: [status]
risk_level: read
required_scope: supplier:read
---
# 按状态查询供应商
## Instructions
- `status` 枚举为 AVAILABLE / UNAVAILABLE（manifest.yaml `supplier_status_enum`）；可用供应商指 AVAILABLE。
- 只读查询，不修改任何数据。
## Examples
- "可用的供应商" → {"status": "AVAILABLE"}
