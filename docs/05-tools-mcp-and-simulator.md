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

V5 全量对齐：25 个接口全部进入决策范围、全部可执行（模拟器 / 云 HTTP / MCP 全链路），写/删工具按审批门控。

| Tool | 风险 | 作用 |
|---|---|---|
| `getProductByName` | READ | 按产品名称查询价格和库存 |
| `getProductById` | READ | 按产品 ID 查询详情 |
| `getProductSubstitutes` | READ | 按 ID 查询替代产品 |
| `getProductSubstitutesByName` | READ | 按名称查询替代产品 |
| `getBatchProductByProductIds` | READ | 按 ID 区间批量查询产品 |
| `getSupplierByStatus` | READ | 按供应商状态筛选候选 |
| `querySuppliersByDeliveryRegion` | READ | 查询指定地区可用供应商 |
| `getSupplierByName` | READ | 按名称查询供应商 |
| `getSupplierById` | READ | 按 ID 查询供应商 |
| `getOrderByOrderId` | READ | 查询订单当前状态 |
| `getOrdersBySupplierId` | READ | 按供应商查询订单 |
| `getByProductId` | READ | 按产品查询订单 |
| `getByOrderStatus` | READ | 按状态查询订单 |
| `getByTimeRange` | READ | 按时间区间查询订单 |
| `createOrder` | WRITE | 创建订单，需要业务校验和确认 |
| `updateOrderStatus` | WRITE | 更新订单状态，需要审批 |
| `cancelOrder` | WRITE | 取消订单，需要审批 |
| `addProduct` | WRITE | 新增产品，需要审批 |
| `addSuppliers` | WRITE | 新增供应商，需要审批 |
| `updateProductDescription` | WRITE | 更新产品描述，需要审批 |
| `updateProductSubstitutes` | WRITE | 更新产品替代品，需要审批 |
| `removeProductByName` | DANGEROUS | 按名称删除产品，强审批 |
| `removeProductById` | DANGEROUS | 按 ID 删除产品，强审批 |
| `deleteSupplierByName` | DANGEROUS | 按名称删除供应商，强审批 |
| `deleteSupplierById` | DANGEROUS | 按 ID 删除供应商，强审批 |

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

### 8.1 真实云 ERP 的 MCP Server（`erp_mcp_server`）

`apps/mcp_gateway/erp_mcp_server.py` 是一个独立 MCP Server，把**真实云 ERP** 的全部 25 个接口（与 §5 工具表完全对齐；写/删工具默认不广告，见下方开关）用 MCP 协议暴露，与内存 `demo_server`（§5）共存。它不是把 ERP 模拟器包一层，而是复用 `apps.worker.executor.build_erp_http_executor`（V5 约定，`X-API-Key` 头，camelCase→snake_case 归一化、302→`ORDER_NOT_FOUND`、`id:-1`→`ORDER_CREATE_FAILED`）——本模块只是 `ToolResult → MCP 响应` 的薄适配，不含任何 ERP 逻辑。

```text
运行:  python -m apps.mcp_gateway.erp_mcp_server
端点:  http://127.0.0.1:8766/mcp   （demo_server 占 8765）
配置:  ERP_API_BASE_URL / ERP_API_KEY（必填，读取真实 ERP）
订单写开关: ERP_MCP_CREATE_ORDER_ENABLED（默认 0，createOrder/updateOrderStatus/cancelOrder）
维护开关:  ERP_MCP_MAINTENANCE_ENABLED（默认 0，addProduct/addSuppliers/updateProduct*/removeProduct*/deleteSupplier*）
```

要点与已知局限：

- **写/删工具默认不注册**（omit 而非 reject）：MCP 无 "disabled tool" 状态，关闭时不把写路径列入 `tools/list`，客户端不会误以为可调用。订单写（createOrder/updateOrderStatus/cancelOrder）由 `ERP_MCP_CREATE_ORDER_ENABLED` 控制，目录增删改（addProduct 等 8 个）由 `ERP_MCP_MAINTENANCE_ENABLED` 控制；真实云下单无法删除，开关需运维显式开启。
- **云端 createOrder 无幂等键**：MCP Server 端不承担 at-most-once，需调用方（如 worker 的 `IdempotencyStore`）保证；响应丢失的重试可能重复下单。
- **`is_retryable` 在 MCP 边界不直接传输**：MCP 无重试信号，失败统一表现为 `is_error=True`（error_code 内嵌在消息文本中）。worker 的 `MCPToolExecutor`（apps/worker/mcp_executor.py）会从文本解析已知瞬态 code（`TIMEOUT`/`UPSTREAM_UNAVAILABLE`/`UPSTREAM_5xx`）并保留为 `error_code`；仅对只读工具还原 `is_retryable` 以驱动 `AsyncRetryExecutor`。外部直连客户端看到的是统一 `is_error=True`，需自行按消息内 code 判断。
- **可重试还原有 write 边界**：瞬态 code 对**所有**工具保留为 `error_code`（recovery 闸门按 code 分类歧义性），但 `is_retryable` 还原仅对 §5 的全部 14 个 READ 工具生效（`apps/worker/mcp_executor.py` 的 `_READ_TOOL_NAMES` allowlist）。云端 `createOrder` 无服务器端幂等，歧义性失败（如超时）保留 code（如 `TIMEOUT`）但 `is_retryable=False`——recover_or_replan 的歧义写闸门据此给出 `WRITE_OUTCOME_AMBIGUOUS` 交人工对账，封死"重规划重调 createOrder"的重复下单窗口；步骤内重试（`AsyncRetryExecutor` 不查 `IdempotencyStore`）也被 `is_retryable=False` 挡住。用 allowlist 而非 blocklist：所有写/删工具（updateOrderStatus/cancelOrder 及维护类）默认同样不自动重试。
- 业务失败（未知产品、`id:-1`、HTTP 5xx）→ 工具抛 `ValueError` → SDK 转 `CallToolResult(is_error=True)`，与 demo_server 同语义。

## 9. 推荐开源参考

- LangGraph：状态机与Checkpoint。
- Official MCP Servers：MCP服务实现方式。
- Online Boutique：商品、购物车和订单业务建模参考。
- Toxiproxy：ERP接口延迟、断连和超时注入。
- API-Bank、ToolBench或BFCL：Tool选择与函数调用评测格式参考。

引用公开数据前必须检查许可证并保留来源说明。
