# 动态模拟数据构建指南

## 1. 目标

构建一个不依赖受限ERP服务、可以重复演示和评测的ERP Simulator。它提供商品、库存、供应商和订单状态，并支持有状态写操作、接口限额和故障注入。

## 2. 数据来源

动态模拟数据主要由项目生成，不从网上实时抓取：

- 商品、库存、供应商和订单根据场景Fixture生成。
- 订单号等非关键字段根据固定随机种子生成。
- 业务状态变化由创建、更新和取消订单Tool驱动。
- 公开ERP或电商项目只用于参考领域模型和API形态。
- 少量真实ERP响应可以脱敏后进入Replay Fixture。

## 3. 目录结构

```text
datasets/simulator/
├── products.yaml
├── suppliers.yaml
├── orders.yaml
├── scenarios/
│   ├── order_happy_path_001.yaml
│   ├── insufficient_inventory_001.yaml
│   └── supplier_unavailable_001.yaml
├── business_rules/
├── response_fixtures/
├── replay/
└── schemas/
```

## 4. 场景Schema

```yaml
scenario_id: order_happy_path_001
title: 上海20KG苹果订单
category: create_order
random_seed: 1001

products:
  - product_id: 1
    name: 苹果
    quantity_in_stock: 100
    unit: KG
    price: 10

suppliers:
  - supplier_id: 3
    name: 华东物流
    regions: [上海, 南京]
    status: AVAILABLE
    rating: 4.8
    delivery_days: 2
    price_per_kg: 1.2

orders: []

expected_plan:
  - getProductByName
  - querySuppliersByDeliveryRegion
  - createOrder
expected_result:
  status: CREATED
  product_id: 1
  quantity: 20
  supplier_id: 3
  order_region: 上海
```

## 5. 业务数据生成

模拟器根据场景和Tool调用维护业务状态：

- 查询商品时返回当前价格和库存。
- 查询供应商时按区域、状态、评分和费用过滤。
- 创建订单成功后扣减库存并新增订单。
- 更新或取消订单时校验订单状态机。

固定`random_seed`保证订单号等生成字段可重复。随机值不能改变库存是否充足、供应商是否可用或订单状态是否合法。

建议业务对象：

- 产品：ID、名称、描述、价格、库存和替代产品。
- 供应商：ID、名称、配送区域、费用、时效、评分和状态。
- 订单：ID、产品、数量、供应商、区域、金额、时间和状态。
- Tool配额：剩余调用次数、重置时间和限额错误。

## 6. Tool响应与业务错误

错误Fixture包含稳定错误码和可变化业务字段：

```json
{
  "error_code": "INSUFFICIENT_INVENTORY",
  "tool": "createOrder",
  "message_template": "Product {product_id} requires {required} KG but only {available} KG is available",
  "business_tag": "inventory"
}
```

评测标准答案依赖`error_code`和业务状态，不依赖随机订单号。列表查询结果需支持分页和最大响应限制。

## 7. 订单与库存状态

保存：

- 产品当前库存和库存单位。
- 供应商配送区域、状态、时效和费用。
- 订单当前状态和合法状态转换。
- 操作时间、操作人和外部操作ID。

创建订单后扣减库存；取消订单是否恢复库存由显式业务规则决定，所有变化写入不可变操作历史。

## 8. 写操作与幂等

每个写请求包含`idempotency_key`。模拟器维护：

```text
idempotency_key
tool_name
arguments_hash
status
result
created_at
```

重复相同请求返回首次结果；相同键但参数不一致返回409。场景重置时清理测试状态，但保留测试运行之外需要审计的数据。

## 9. 故障注入

Tool级故障配置：

```yaml
faults:
  getProductByName:
    fail_first_n: 1
    error_type: timeout
    latency_ms: 12000
  querySuppliersByDeliveryRegion:
    response_status: 200
  createOrder:
    commit_then_timeout: true
```

`commit_then_timeout`用于测试最危险的情况：订单已经创建，但客户端没有收到响应。恢复逻辑必须按幂等键或订单查询先对账，不能直接再次创建。

## 10. ERP Record/Replay

ERP接口有调用次数限制，因此数据构建采用一次采样、多次回放：

1. 从OpenAPI生成Schema，不调用ERP。
2. 为核心读取接口选择少量代表参数。
3. 以LIVE模式采集一次响应。
4. 删除密钥、用户信息和不稳定字段。
5. 保存请求指纹、响应、状态码和采集时间。
6. 后续开发和评测使用REPLAY。

Fixture示例：

```json
{
  "fixture_id": "get_product_by_name_apple_success",
  "tool": "getProductByName",
  "request": {"name": "苹果"},
  "response_status": 200,
  "response": {
    "productId": 1,
    "name": "苹果",
    "quantityInStock": 100
  }
}
```

429、超时、5xx和危险写操作由模拟器构造，不需要消耗真实额度。

## 11. 场景质量检查

- 产品、库存、供应商、订单和业务规则相互一致。
- 场景具有至少一个业务分支，防止只靠单一关键词选择Tool。
- 写操作可以改变状态并由查询Tool验证。
- 同一随机种子结果稳定。
- 所有写操作可以重置和重复评测。
- ERP Tool错误覆盖超时、429、5xx、业务失败和写入状态未知。

## 12. 推荐规模

第一版完成3个核心场景；完整就绪版本扩展到6至8个：

- 库存充足的标准下单。
- 库存不足与替代产品。
- 配送区域无可用供应商。
- 产品、数量或地区缺参。
- 订单状态非法转换。
- 订单创建已提交但响应超时。
- 恶意或越权Tool请求。
- Worker中断与订单写操作恢复。
