---
document_id: domain-product-catalog
document_type: domain_model
business_domains: [product]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 商品目录

## 概述

商品目录定义了 ERP 系统中所有可售商品的静态信息和库存单位。当前共 6 个商品，分为生鲜和电子产品两大类。

## 商品字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| product_id | integer | 是 | 商品唯一标识，1-6 的整数 |
| name | string | 是 | 商品中文名称 |
| description | string | 否 | 商品简短描述 |
| price | float | 是 | 单价，单位元（CNY），保留两位小数 |
| quantity_in_stock | integer | 是 | 当前库存量，下单成功后会扣减相应的数量 |
| unit | string | 是 | 库存单位，必须是 product_unit_enum 中的枚举值 |

## 库存单位枚举

| 单位 | 适用商品 |
|------|----------|
| KG | 苹果、香蕉、橙子（生鲜类按重量计） |
| 台 | 电脑（按整机计） |
| 件 | 键盘、鼠标（按个数计） |

**约束**：不接受 "千克""公斤""个""箱" 等模糊单位。必须精确映射为标准枚举值。

## 商品清单

### 生鲜类

| product_id | 名称 | 描述 | 单价（元） | 初始库存 | 单位 |
|------------|------|------|-----------|---------|------|
| 1 | 苹果 | 新鲜红富士苹果 | 10.00 | 100 | KG |
| 2 | 香蕉 | 进口香蕉 | 8.00 | 50 | KG |
| 3 | 橙子 | 赣南脐橙 | 12.00 | 80 | KG |

### 电子产品

| product_id | 名称 | 描述 | 单价（元） | 初始库存 | 单位 |
|------------|------|------|-----------|---------|------|
| 4 | 电脑 | 办公笔记本电脑 | 5000.00 | 15 | 台 |
| 5 | 键盘 | 机械键盘 | 200.00 | 30 | 件 |
| 6 | 鼠标 | 无线鼠标 | 100.00 | 50 | 件 |

## 查询方式

- **按名称查询**：`GET /products/{name}`，支持中文精确匹配
- **按 ID 查库存**：`GET /products/{product_id}/stock`，返回实时的库存量

## 注意事项

- 价格是历史快照值，实时价格以 API 返回为准。
- 库存量是运行时状态，下单后会减少。manifest.yaml 中记录的是初始值。
- 生鲜类商品的保质期和批次信息在当前版本中不建模。
