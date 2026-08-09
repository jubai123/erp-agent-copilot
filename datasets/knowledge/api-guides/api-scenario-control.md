---
document_id: api-scenario-control
document_type: api_guide
business_domains: [product, supplier, order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# API：场景控制

## 端点

### 查询当前场景

```
GET /scenario
```

**返回值**：

```json
{"scenario": "happy_path"}
```

### 切换场景

```
PUT /scenario/{scenario_id}
```

**返回值**：

```json
{"scenario": "stock_insufficient"}
```

## 参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| scenario_id | path | string | 是 | 有效值：happy_path, stock_insufficient, supplier_unavailable, timeout |

## 错误码

| 状态码 | 说明 |
|--------|------|
| 422 | 无效的 scenario_id |

## 调用约束

- 场景切换是全局的，影响所有后续请求。
- 生产环境中不应暴露此接口（仅用于开发/测试）。
- Agent 正常业务流程中不调用此接口（仅测试/评测使用）。
- 此接口不影响历史数据（已创建的订单不受场景切换影响）。

## 与其他接口的关系

- 切换场景后立即生效，所有受影响的端点行为改变。
- 测试中通常在 setup 阶段调用，测试结束后复位为 happy_path。
- Agent 在异常场景下的行为是评测重点：能否识别库存不足、供应商不可用、超时并给出合理提示。
