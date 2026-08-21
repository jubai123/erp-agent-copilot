---
name: supplier-create
description: 添加新供应商到目录。触发词：添加供应商、新增供应商、注册供应商。写操作，必须走审批。
tool: addSuppliers
required_params: [name, regions, status]
risk_level: write
required_scope: order:write
---
# 添加供应商
## Instructions
- 写操作：风险 WRITE，需 `order:write` scope，执行前必须通过审批。
- `name` 是供应商名称，`regions` 是覆盖的配送区域列表（region 枚举：上海、南京、北京等）。
- `status` 为 AVAILABLE 或 UNAVAILABLE。
## Examples
- "添加一个覆盖上海和南京、状态可用的新供应商" → {"name": "新供应商", "regions": ["上海", "南京"], "status": "AVAILABLE"}
