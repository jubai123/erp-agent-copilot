---
name: supplier-by-region-query
description: 按配送区域查询供应商。用户给出城市并要求该区域可配送的供应商时使用。触发词：上海有哪些供应商、谁能配送、当地供货。
tool: querySuppliersByDeliveryRegion
required_params: [region]
risk_level: read
required_scope: supplier:read
---
# 按配送区域查询供应商
## Instructions
- `region` 必须在 manifest.yaml `supplier_region_enum` 内：上海 / 南京 / 北京 / 天津 / 广州 / 深圳 / 成都 / 重庆 / 西安 / 兰州。
- 返回该区域供应商列表；可能为空——空列表是合法业务答案，不代表失败。
- 只读查询，不修改任何数据。
## Examples
- "上海有哪些供应商" → {"region": "上海"}
