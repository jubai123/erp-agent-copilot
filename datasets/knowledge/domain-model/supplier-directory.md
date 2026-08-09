---
document_id: domain-supplier-directory
document_type: domain_model
business_domains: [supplier]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 供应商目录

## 概述

供应商目录定义了 ERP 系统中所有物流供应商的静态信息和服务能力。当前共 5 个供应商，覆盖中国 10 个城市的配送服务。

## 供应商字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| supplier_id | integer | 是 | 供应商唯一标识，3-7 的整数 |
| name | string | 是 | 供应商中文名称 |
| regions | list[string] | 是 | 覆盖的配送区域列表，每项必须在 supplier_region_enum 中 |
| status | string | 是 | 当前状态：AVAILABLE（可接单）或 UNAVAILABLE（不可用） |
| rating | float | 否 | 服务质量评分，范围 0-5，越高越好 |
| delivery_days | integer | 否 | 平均配送天数 |
| price_per_kg | float | 否 | 每公斤配送单价（元） |

## 供应商状态枚举

| 状态 | 含义 | 行为 |
|------|------|------|
| AVAILABLE | 供应商可正常接单 | 返回报价和时效，可用于创建订单 |
| UNAVAILABLE | 供应商不可用 | 不可向其创建订单，需提示用户选择其他供应商 |

**场景影响**：`supplier_unavailable` 场景下，所有供应商的状态强行返回 UNAVAILABLE，无论种子数据如何。

## 配送区域枚举

当前支持的配送城市（10 个）：

| 区域 | 所属地区 |
|------|----------|
| 上海 | 华东 |
| 南京 | 华东 |
| 北京 | 华北 |
| 天津 | 华北 |
| 广州 | 华南 |
| 深圳 | 华南 |
| 成都 | 西南 |
| 重庆 | 西南 |
| 西安 | 西北 |
| 兰州 | 西北 |

**约束**：region 参数必须精确匹配以上枚举值，不接受 "上海市""北京城区" 等变体。

## 供应商清单

| supplier_id | 名称 | 配送区域 | 状态 | 评分 | 配送天数 | 单价（元/kg） |
|-------------|------|----------|------|------|---------|-------------|
| 3 | 华东物流 | 上海、南京 | AVAILABLE | 4.8 | 2 | 1.20 |
| 4 | 华北物流 | 北京、天津 | AVAILABLE | 4.5 | 3 | 1.00 |
| 5 | 华南物流 | 广州、深圳 | UNAVAILABLE | 4.2 | 1 | 1.50 |
| 6 | 西南物流 | 成都、重庆 | AVAILABLE | 4.0 | 3 | 1.30 |
| 7 | 西北物流 | 西安、兰州 | AVAILABLE | 3.8 | 5 | 0.90 |

## 查询方式

- **按区域和状态筛选**：`GET /suppliers?region={region}&status={status}`
- 两个查询参数均为可选，不传则不过滤

## 供应商选择原则

1. 必须覆盖用户指定的配送区域。
2. 首选 status=AVAILABLE 的供应商。
3. 在可用供应商中，按评分降序、配送天数升序、价格升序综合排序。
4. 不可用供应商不可下单，必须提示用户选择其他供应商或等待恢复。
