# API 契约：ERP Agent Copilot V6

> 最终交付文档。反映**当前已实现**的 HTTP 接口；请求/响应结构与 `apps/api/schemas/` 一致。详细设计见 `docs/04-knowledge-and-retrieval.md`、`docs/07-security-approval-and-recovery.md`。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| 基础路径 | 平台 API `/v1`；Simulator / Gateway 无版本前缀 |
| 数据格式 | 请求/响应均为 `application/json` |
| 时间格式 | ISO 8601（如 `2026-08-09T10:00:00`） |
| 错误模型 | 统一 `{ "detail": "<message>" }`（FastAPI 默认），业务错误码见 §2 |
| OpenAPI | 各进程启动后可访问 `/docs`（Swagger UI） |

## 2. 错误模型

领域层统一错误基类 `CopilotError`（[errors.py](../src/erp_copilot/domain/errors.py)），携带 `message`（人读）、`code`（机读）、`details`（上下文）。

| 子类 | code | 语义 | HTTP 映射 |
|---|---|---|---|
| `ValidationError` | `VALIDATION_ERROR` | 输入校验/ Schema 不匹配 | 422 |
| `AuthorizationError` | `AUTHORIZATION_ERROR` | 权限不足或 Scope 不满足 | 403 |
| `NotFoundError` | `NOT_FOUND` | 资源不存在 | 404 |
| `ToolExecutionError` | `TOOL_EXECUTION_ERROR` | Tool 执行失败 | 502 |
| `RetrievalError` | `RETRIEVAL_ERROR` | 知识/文档检索失败 | 500 |
| `AgentRuntimeError` | `AGENT_RUNTIME_ERROR` | 规划/执行/验证失败 | 500 |
| `IdempotencyConflictError` | `IDEMPOTENCY_CONFLICT` | 同幂等键写入已在进行 | 409 |
| `ConfigError` | `CONFIG_ERROR` | 配置缺失/非法 | 500 |

> 说明：当前 FastAPI 路由层对大部分错误直接抛 `HTTPException`（`detail` 为消息字符串），尚未统一包一层 `{code, message, details}` JSON；这是后续可收紧的契约点。

## 3. 平台 API（`apps/api`，uvicorn :8000）

### 3.1 `GET /health`

```json
{ "status": "ok", "app_name": "erp-agent-copilot", "version": "0.1.0" }
```

### 3.2 `POST /v1/tools/import/openapi`

从 OpenAPI 3.x 文档导入工具，按租户版本化注册。

请求：
```json
{
  "tenant_id": "t-1",
  "spec": { "...OpenAPI 3.x JSON..." }
}
```

响应 `200`：
```json
{
  "imported": [
    {
      "id": "…", "name": "getProducts", "description": "…",
      "current_version": 1, "risk_level": "read", "is_active": true
    }
  ]
}
```

错误：`422`（spec 非 OpenAPI JSON / 缺 tenant_id / 导入校验失败）。

### 3.3 `GET /v1/tools?tenant_id=t-1`

列出租户工具（当前版本、风险分级）。

响应 `200`：`[ToolResponse, …]`（结构同 §3.2 的 `imported` 项）。

### 3.4 `POST /v1/runs`（创建任务，异步）

```json
{ "tenant_id": "t-1", "title": "下单", "product_name": "苹果" }
```

响应 `202`（异步受理，`run_id` 用于轮询）：
```json
{
  "run_id": "…", "tenant_id": "t-1", "title": "下单",
  "status": "QUEUED", "created_at": "2026-08-09T10:00:00"
}
```

行为：写 `runs` 行（初始状态 `QUEUED`）→ `execute_run.delay(run_id, product_name)` 入 Celery 队列。

### 3.5 `GET /v1/runs/{run_id}`

查询 Run 当前状态。响应 `200` 结构同 §3.4；`404`：Run 不存在。

### 3.6 `POST /v1/runs/{run_id}/approve`

审批/拒绝暂停的写步骤并恢复执行（`docs/07 §8`）。租户边界取自 `runs` 行而非客户端。

```json
{
  "step_id": "s2",
  "decision": "APPROVE",
  "decided_by": "approver@corp.com",
  "reason": "库存充足，同意下单",
  "trace_id": "…"
}
```

- `decision`：`"APPROVE" | "DENY"`（唯一枚举）。
- 响应 `200`：`{ run_id, step_id, decision, decided_by, decided_at, run_status }`。
- `404`：Run 不存在 / 步骤不存在；`409`：决策状态冲突（如已决策）。

### 3.7 `POST /v1/knowledge/search`（⚠️ 当前为 STUB）

请求：
```json
{ "query": "苹果库存查询规则", "tenant_id": "t-1", "top_k": 5 }
```

响应 `200`：`{ "results": [], "citations": "" }`。

> **诚实标注**：该端点骨架已就位（FTS → 语义 → RRF → Rerank → 引用装配的调用点已注释在路由内），但**当前返回空结果集**——数据库未灌入知识文档时直接回退为空，属占位实现。真实检索管线在 `src/erp_copilot/retrieval/` 已有组件与单测，接入该端点是后续集成点。

### 3.8 `GET /metrics`

Prometheus 文本格式指标（`runs_created/completed/failed`、`phase_latency` 直方图、`worker_queue` 仪表）。

## 4. ERP Simulator（`apps/erp_simulator`，uvicorn :8001）

确定性 ERP 后端，模拟产品/库存/供应商/订单与场景切换，避免依赖有调用次数限制的真实系统。

| 接口 | 说明 |
|---|---|
| `GET /health` | `{"status":"ok","service":"erp-simulator"}` |
| `GET /products/{name}` | 按名称查产品（含当前库存、单价、单位） |
| `GET /products/{id}/stock` | 按 ID 查实时库存 |
| `GET /suppliers?region=上海&status=ACTIVE` | 按地区筛供应商（含评级、配送天数、单价） |
| `POST /orders` | 创建订单（库存校验 + 幂等，见下） |
| `GET /orders/{order_id}` | 查订单详情 |
| `GET /scenario` | 当前场景 |
| `PUT /scenario/{name}` | 切换场景：`happy_path` / `stock_insufficient` / `supplier_unavailable` / `timeout` |

### 4.1 `POST /orders` 契约

```json
{
  "product_id": 1, "quantity": 20,
  "supplier_id": 3, "region": "上海",
  "idempotency_key": "run-s2-9f8e…"
}
```

| 状态码 | 语义 |
|---|---|
| `201` | 创建成功，返回订单（含 `order_id`、`amount`、`status`） |
| `200` | 幂等命中：同 `idempotency_key` 已创建，返回**同一订单**，库存不再扣减 |
| `422` | 库存不足（`Insufficient stock: …`）或产品不存在 |
| `504` | 场景为 `timeout`（模拟 ERP 超时，可重试） |

## 5. MCP Gateway（`apps/mcp_gateway`，uvicorn :8002）

| 接口 | 说明 |
|---|---|
| `GET /health` | `{"status":"ok","service":"mcp-gateway"}` |
| `GET /servers` | `{"servers": ["…已注册 MCP 服务器…"]}` |

`MCPGateway`（`src/erp_copilot/tools/mcp_gateway.py`）负责持久 MCP 连接（惰性连接、自动重连、`execute_tool`）；HTTP 层当前仅暴露健康检查与服务器清单。

## 6. 核心数据实体

持久层由 Alembic 迁移维护（`migrations/versions/`），表清单：

| 表 | 用途 |
|---|---|
| `tenants` / `users` / `roles` / `role_scopes` | 租户、用户、角色与权限 Scope |
| `tools` / `tool_versions` / `tool_parameters` | 工具注册、版本化与参数元数据 |
| `runs` / `run_steps` / `run_events` | Run 生命周期、步骤结果、事件审计 |
| `agent_checkpoints` | 每个图节点后的完整 `AgentState` 快照 |
| `idempotency_records` | 写操作幂等记录（`uq_idempotency_tenant_key` 唯一约束） |
| `knowledge_documents` / `document_chunks` | 知识库文档与分块（含向量列） |
| `security_events` | 注入/越权/SSRF 拦截事件 |
| `audit_logs` | 审批等敏感操作审计 |

### Run 状态机

`AgentState` 生命周期 `AgentStatus`：`QUEUED → PLANNING → WAITING_APPROVAL → EXECUTING → VERIFYING → SUCCEEDED`，含 `RETRYING / REPLANNING / FAILED / CANCELLED / EXPIRED`；`runs` 表持久化 `RunStatus`（`pending / running / waiting_approval / completed / failed / cancelled`），由 Checkpoint 映射。

工具风险分级 `ToolRiskLevel`：`read < write < dangerous`（写操作需审批，`dangerous` 额外门控）。
