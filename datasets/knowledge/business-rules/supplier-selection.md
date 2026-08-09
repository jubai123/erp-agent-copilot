---
document_id: rule-supplier-selection
document_type: business_rule
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

# 业务规则：供应商选择

## 规则描述

下单前必须筛选和验证供应商，不可向不可用供应商下单。

## 筛选流程

1. 用户指定配送区域（region）。
2. Agent 调用 `GET /suppliers?region={region}` 过滤该区域的供应商。
3. 从结果中仅保留 `status=AVAILABLE` 的供应商。
4. 按以下优先级排序：评分（高→低）→ 配送天数（少→多）→ 价格（低→高）。
5. 向用户展示候选供应商列表（名称、评分、配送天数、价格）。
6. 用户选择后，使用选定的 supplier_id 下单。

## 无可用供应商的处理

- 若指定区域无供应商覆盖：告知用户该区域暂不支持配送。
- 若供应商全为 UNAVAILABLE：告知用户当前无可用物流，建议稍后重试。
- **不得**编造不存在的 supplier_id。
- **不得**跨区域选择供应商（如用户指定"北京"，不应推荐只覆盖"上海"的供应商）。

## 场景影响

| 场景 | 行为 |
|------|------|
| happy_path | 返回种子数据中的实际状态 |
| supplier_unavailable | 所有供应商 status=UNAVAILABLE |

## 供应商排序示例

查询 region=上海：

| 供应商 | 状态 | 评分 | 配送天数 | 价格 | 综合排序 |
|--------|------|------|---------|------|---------|
| 华东物流 | AVAILABLE | 4.8 | 2 | 1.20 | **第 1** |
| (其他) | — | — | — | — | 不覆盖上海 |

结果：仅华东物流，推荐使用。

## 验证标准

- 选择 AVAILABLE 供应商 → 下单成功。
- 选择 UNAVAILABLE 供应商 → Agent 应拦截，提示不可用。
- supplier_unavailable 场景 → Agent 应识别无可用供应商，告知用户。
