# Tool、MCP与ERP Simulator设计

> 架构决策摘要见 [11-architecture-decisions.md](11-architecture-decisions.md)。本文件对应其中决策五（工具候选过滤）。

## 0. 工具候选过滤

`build_plan` 使用两层工具候选过滤，缩小 LLM 的工具选择范围：

```text
第一级：意图→域确定性过滤（主引擎，必过）
  classify_intent 输出 (domain, action)
    → DOMAIN_TOOL_MAP 精确映射（不需要 Embedding/Rerank）
    → 候选 3-8 个，直接进 build_plan Prompt

第二级：向量检索精排（按需，仅当候选 > 阈值时启用）
  候选数 > 5-8 或描述 Token 超预算时
    → 在候选集合内做向量粗筛 + Rerank 精排
    → Top-N 进 Prompt
```

**第一级是主引擎**：V6 当前 9 个工具，`(domain, action)` 过滤后候选 3-5 个，`build_plan` 的犯错面从 25 选 1 缩小到 3 选 1。

**第二级是增长路径**：当单个域的工具数超过 5-8 个时启用。架构上预留接口，评测中保留基线，但不为当前不需要的复杂度提前实现。

**这是 V5 两阶段检索的重定义，不是放弃**：

| | V5 | V6 |
|---|---|---|
| 主引擎 | Milvus 向量检索（每次必走） | 意图→域确定性过滤 |
| 向量检索角色 | 唯一入口 | 第二级按需精排 |
| 存储 | Milvus + MongoDB | pgvector + PostgreSQL |
| 工具意图训练数据 | 微调 Embedding | 转为 40 条 Tool 检索评测 Case |

## 1. Tool Registry

每个Tool采用不可变版本管理。一次Run绑定具体`tool_version_id`，避免执行过程中Schema变化。

核心字段：

- Tool名称、版本、来源和描述。
- OpenAPI或MCP Schema。
- 参数Schema和输出Schema。
- 风险级别、所需Scope和审批策略。
- 超时、最大响应大小和重试策略。
- 允许访问的主机、协议和端口。
- 是否支持幂等键、健康状态和停用状态。

风险级别：

- `READ`：查询商品、库存、供应商和订单信息。
- `WRITE`：创建订单、更新订单状态和维护业务资料。
- `ADMIN`：删除商品、删除供应商和权限变更等高风险操作。

## 2. OpenAPI导入

导入器必须：

- 只接受支持的OpenAPI 3.x版本。
- 解析Server、Path、Method、Parameter、RequestBody和Response。
- 拒绝重复Operation ID、无描述Tool和无法解析的Schema。
- 规范化Tool名称，保留原始定义和校验报告。
- 不根据“查询”“获取”等名称猜测风险，风险由显式规则和人工确认决定。
- 导入新版本时不覆盖正在运行的旧版本。

V5的25个ERP OpenAPI接口是V6 Tool Registry、检索评测和业务场景构建的直接基线；V6在其上补充版本、风险、Scope、幂等和模拟响应。

## 3. MCP Gateway

Gateway提供统一执行接口：

```text
execute(tool_version_id, arguments, run_context) -> ToolResult
```

`run_context`至少包含：

- `run_id`、`step_id`、`tenant_id`和`user_id`。
- Trace上下文。
- `idempotency_key`。
- deadline和取消标识。
- 授权Scope与审批ID。

Gateway负责连接复用、能力缓存、健康检查、超时、错误映射、响应截断和安全校验。Agent节点不得直接调用`requests`或任意URL。

## 4. ToolResult契约

```json
{
  "status": "SUCCEEDED",
  "tool_version_id": "tool-version-id",
  "started_at": "2026-07-20T10:25:00Z",
  "finished_at": "2026-07-20T10:25:01Z",
  "attempt": 1,
  "data": {},
  "artifact_uri": null,
  "error": null,
  "metadata": {
    "http_status": 200,
    "remaining_quota": 98
  }
}
```

Tool错误统一映射为暂时性、永久性、鉴权、限额、超时、取消或安全拦截。

## 5. ERP Simulator工具

首版提供：

| Tool | 风险 | 作用 |
|---|---|---|
| `getProductByName` | READ | 按产品名称查询价格和库存 |
| `getProductById` | READ | 按产品ID查询详情 |
| `getProductSubstitutesByName` | READ | 库存不足时查询替代产品 |
| `querySuppliersByDeliveryRegion` | READ | 查询指定地区可用供应商 |
| `getSupplierByStatus` | READ | 按供应商状态筛选候选 |
| `getOrderByOrderId` | READ | 查询订单当前状态 |
| `createOrder` | WRITE | 创建订单，需要业务校验和确认 |
| `updateOrderStatus` | WRITE | 更新订单状态，需要审批 |
| `cancelOrder` | ADMIN | 取消订单，强制确认 |

## 6. 场景与状态

模拟器根据`scenario_id`加载确定性状态：

- `order_happy_path`：库存充足、存在多个上海供应商，可以创建订单。
- `insufficient_inventory`：苹果库存不足，需要停止下单或查询替代产品。
- `supplier_unavailable`：目标区域没有可用供应商，需要用户确认其他方案。
- `missing_order_parameter`：缺少产品、数量、地区或供应商信息。
- `tool_timeout`：指定ERP Tool在前N次调用超时。
- `write_committed_timeout`：订单已创建但响应超时，用于验证对账和幂等。

每个场景使用固定`random_seed`生成订单号等非关键字段，但库存判断、供应商候选和预期业务结果不变。

## 7. 有状态写操作

- 所有写Tool必须接受`idempotency_key`。
- 模拟器保存操作记录和结果摘要。
- 相同键和相同参数返回首次结果。
- 相同键但参数不同返回冲突错误。
- 每个评测Case执行前可重置到初始场景状态。

## 8. ERP调用限额处理

真实ERP仅用于少量兼容性和Smoke Test，不参与日常评测。

执行模式：

```text
MOCK   → 完全使用本地构造响应
REPLAY → 回放已脱敏的真实响应Fixture
LIVE   → 调用真实ERP，仅限显式开启
```

建议流程：

1. 从OpenAPI直接构建Tool Schema，不消耗额度。
2. 每个核心读取Tool仅采集少量正常响应。
3. 空结果、429、超时、5xx和危险参数由模拟器构造。
4. 写操作全部在模拟器执行，不调用真实ERP。
5. CI和离线Eval强制禁止`LIVE`模式。

执行器读取配额响应头并记录`remaining_quota`，达到阈值后拒绝非必要LIVE调用。对429使用受限退避，不对写操作进行无幂等保障的自动重试。

## 9. 推荐开源参考

- LangGraph：状态机与Checkpoint。
- Official MCP Servers：MCP服务实现方式。
- Online Boutique：商品、购物车和订单业务建模参考。
- Toxiproxy：ERP接口延迟、断连和超时注入。
- API-Bank、ToolBench或BFCL：Tool选择与函数调用评测格式参考。

引用公开数据前必须检查许可证并保留来源说明。
