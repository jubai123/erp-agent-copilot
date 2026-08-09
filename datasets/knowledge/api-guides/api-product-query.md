---
document_id: api-product-query
document_type: api_guide
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

# API：按名称查询商品

## 端点

```
GET /products/{name}
```

## 功能

通过商品中文名称精确查询商品信息（名称、描述、价格、库存、单位）。

## 参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| name | path | string | 是 | 商品中文名称，必须精确匹配。支持值：苹果、香蕉、橙子、电脑、键盘、鼠标 |

## 返回值

**200 OK**：

```json
{
  "product_id": 1,
  "name": "苹果",
  "description": "新鲜红富士苹果",
  "price": 10.0,
  "quantity_in_stock": 100,
  "unit": "KG"
}
```

## 错误码

| 状态码 | 说明 |
|--------|------|
| 404 | 商品不存在（name 不在 6 个商品名称中） |

## 调用约束

- name 参数是中文精确匹配，不支持模糊搜索。
- 不区分大小写（但商品名全为中文，无关紧要）。
- 该端点不受场景切换影响（始终返回种子数据中的静态信息）。
- 库存量 `quantity_in_stock` 是运行时状态，在 happy_path 下反映实际库存。

## 与其他接口的关系

- 下单前通常先调用此接口确认商品信息和当前库存。
- `GET /products/{product_id}/stock` 只返回库存量，不返回商品详情。
- 该端点不受 `stock_insufficient` 场景影响（始终返回种子库存值），如需场景感知的库存查询请使用 `/products/{id}/stock`。
