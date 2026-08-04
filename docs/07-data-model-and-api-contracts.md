# 数据模型与API契约

## 1. 建模原则

- 所有业务表包含`tenant_id`，查询默认带租户过滤。
- 核心状态采用Enum和数据库约束，不使用无含义整数。
- Run状态变化使用追加事件，不能只覆盖最终状态。
- Tool和知识文档采用不可变版本。
- 时间统一保存UTC，API使用ISO 8601。
- API输入输出使用Pydantic Schema，领域层不依赖Schema类。

## 2. 身份与权限表

### tenants

`id`、`name`、`status`、`created_at`。

### users

`id`、`tenant_id`、`username`、`password_hash`、`status`、`created_at`。

### roles、role_scopes、user_roles

保存角色、Scope集合及用户角色关系。唯一约束防止重复授权。

## 3. Tool表

### tools

保存Tool稳定身份：`id`、`tenant_id`、`name`、`description`、`status`。

### tool_versions

保存版本化Schema：

- `id`、`tool_id`、`version`。
- `source_type`：OpenAPI、MCP或Internal。
- `input_schema`、`output_schema`。
- `endpoint_config`和原始定义引用。
- `risk_level`、`timeout_s`、`max_response_bytes`。
- `supports_idempotency`、`created_at`。

### tool_scopes

保存Tool版本需要的Scope和审批策略。

## 4. 知识库表

### knowledge_documents

保存文档身份、版本、元数据、信任级别、访问Scope、校验和和发布状态。

### document_chunks

保存Chunk文本、标题路径、全文检索字段、Embedding、Token数和引用位置。

### ingestion_jobs

保存导入状态、文件信息、处理数量、失败原因、开始和结束时间。

## 5. Run与Step表

### runs

核心字段：

- `id`、`tenant_id`、`user_id`。
- `query`、`intent`、`risk_level`。
- `status`、`version`和`deadline_at`。
- `plan_version`、`result_summary`。
- `created_at`、`started_at`、`finished_at`。

### run_steps

- `run_id`、`step_id`、`tool_version_id`。
- `depends_on`、`arguments`和`argument_sources`。
- `status`、`attempt_count`和`idempotency_key`。
- `success_condition`、`result_summary`、`artifact_id`。
- `started_at`、`finished_at`和`error_code`。

`run_id + step_id`唯一；`idempotency_key`对写Tool建立唯一约束。

### run_events

追加保存状态迁移、节点开始结束、审批、重试、取消和恢复事件。

### run_artifacts

保存Artifact URI、内容类型、大小、校验和、敏感级别和访问Scope。

## 6. 审批与安全表

### approvals

保存Run、Step、Plan版本、参数哈希、审批人、决策、理由和过期时间。

### audit_logs

保存身份、资源、动作、结果、IP、Trace ID和不可变时间戳。

### security_events

保存攻击类型、检测层、严重级别、输入摘要、处置结果和关联Run。

## 7. 评测表

- `eval_cases`：输入、场景、标准文档、标准Tool、预期结果和标签。
- `eval_runs`：模型、Prompt、检索配置、代码版本和启动参数。
- `eval_results`：Case结果、各项得分、延迟、成本和失败分类。

## 8. 核心API

```text
POST   /v1/runs
GET    /v1/runs/{run_id}
GET    /v1/runs/{run_id}/events
POST   /v1/runs/{run_id}/approve
POST   /v1/runs/{run_id}/cancel

POST   /v1/tools/import/openapi
GET    /v1/tools
GET    /v1/tools/{tool_id}/versions

POST   /v1/knowledge/ingest
GET    /v1/knowledge/jobs/{job_id}
POST   /v1/knowledge/search

POST   /v1/evals/run
GET    /v1/evals/{eval_run_id}
```

## 9. Run提交示例

```json
{
  "query": "库存充足的话，请创建一个发往上海的20KG苹果订单",
  "scenario_id": "order_happy_path_001",
  "deadline_seconds": 120,
  "execution_mode": "REPLAY"
}
```

响应：

```json
{
  "run_id": "uuid",
  "status": "QUEUED",
  "events_url": "/v1/runs/uuid/events"
}
```

## 10. 审批示例

```json
{
  "approval_id": "uuid",
  "decision": "APPROVE",
  "reason": "已核对产品、数量、供应商和配送区域，同意创建订单",
  "plan_version": 3,
  "parameter_hash": "sha256:..."
}
```

服务端不能相信客户端提交的用户、租户、Scope和风险级别，这些字段从认证上下文和数据库读取。

## 11. 错误响应

```json
{
  "error": {
    "code": "APPROVAL_REQUIRED",
    "message": "The requested operation requires approval.",
    "request_id": "uuid",
    "details": {}
  }
}
```

错误码应稳定，HTTP状态与领域错误一致。内部异常、密钥和堆栈不得返回客户端。

## 12. SSE事件

```text
event: run.step.completed
id: event-uuid
data: {"run_id":"...","step_id":"query_inventory","status":"SUCCEEDED"}
```

客户端使用最后事件ID恢复连接。SSE断开不能取消后台Run。
