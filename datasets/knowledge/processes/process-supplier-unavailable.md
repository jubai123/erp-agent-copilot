---
document_id: process-supplier-unavailable
document_type: business_process
business_domains: [supplier, order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 供应商不可用处理流程

## 适用条件

- 用户指定的配送区域无可用供应商。
- 或系统处于 supplier_unavailable 场景（所有供应商 UNAVAILABLE）。

## 处理步骤

1. **查询供应商**：调用 `GET /suppliers?region={region}`。
2. **筛选状态**：检查返回结果中的 status 字段。
3. **分情况处理**：

### 区域无供应商覆盖

- 候选供应商列表为空（无供应商覆盖该 region）。
- 告知用户："{region} 暂不支持配送。目前支持的城市：上海、南京、北京、天津、广州、深圳、成都、重庆、西安、兰州。"

### 供应商全不可用

- 候选供应商列表非空，但所有 status=UNAVAILABLE。
- 告知用户："{region} 当前所有供应商不可用，建议稍后重试。"
- 可选择性列出不可用供应商名称（供用户参考）。

### 部分供应商可用（正常情况）

- 仅展示 status=AVAILABLE 的供应商。
- 不可用的供应商不应出现在候选列表中。

## 禁止行为

- **不得**在无可用供应商时编造 supplier_id。
- **不得**跨区域选择供应商（如用户指定"北京"，不能推荐只覆盖"广州"的华南物流）。
- **不得**将 UNAVAILABLE 供应商作为有效选项呈现给用户。
- **不得**建议用户"试试看"（明知不可用仍下单）。

## 处置原则

- 供应商不可用不等同于系统故障，是一种预期的业务状态。
- supplier_unavailable 场景下，所有 5 个供应商均为 UNAVAILABLE（包括种子数据中标为 AVAILABLE 的）。
- Agent 应区分"区域不配送"和"供应商暂停服务"两种不同情况。

## 验证标准

- 正常区域 + 有可用供应商 → 正常推荐。
- 正常区域 + 全不可用 → Agent 提示无可用供应商，不下单。
- 未覆盖区域 → Agent 提示该区域不支持配送。
- supplier_unavailable 场景 → Agent 识别全不可用，不下单。
