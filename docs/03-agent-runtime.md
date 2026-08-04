# Agent Runtime设计

## 1. 设计目标

Agent Runtime必须把模型的不确定输出限制在可校验的结构中。Planner只能生成Plan，Executor只能执行已校验和授权的Step，Verifier只能依据证据与成功条件判断结果。

## 2. AgentState

建议使用Pydantic或TypedDict定义强类型状态，至少包含：

```text
run_id, tenant_id, user_id, query
intent, risk_level, plan, current_step
retrieved_context, tool_results, artifacts
working_memory, conversation_summary, context_budget
approval, retry_count, replan_count, errors
status, started_at, deadline_at
```

状态中只保存必要的小型结果。大型日志、Trace或Tool响应写入Artifact，状态只保留URI、摘要、大小和校验和。

## 3. Run状态机

```text
QUEUED
  → PLANNING
  → WAITING_APPROVAL
  → EXECUTING
  → VERIFYING
  → SUCCEEDED

异常分支：
  → RETRYING
  → REPLANNING
  → FAILED
  → CANCELLED
  → EXPIRED
```

状态变化必须同时满足：

- 使用领域方法校验合法迁移。
- 追加一条不可变`run_event`。
- 使用`runs.version`乐观锁防止重复Worker更新。

## 4. LangGraph节点

```text
classify_intent
  → retrieve_context
  → build_plan
  → validate_plan
  → policy_check
  → request_approval
  → execute_ready_steps
  → verify_results
  → recover_or_replan
  → finalize
```

### classify_intent

- 判断任务类型、业务域和初始风险。
- 提取服务、环境、时间范围等显式实体。
- 不生成最终Tool参数。

### retrieve_context

- 检索ERP业务规则、API说明、字段语义、订单流程和异常处理文档。
- 对检索结果执行租户、ACL和可信度过滤。
- 保存引用位置和检索分数。

### build_plan

- 只输出符合Schema的Plan DAG。
- 不能调用Tool，也不能绕过Policy。
- 每个参数必须记录来源或待确认状态。

### validate_plan

- 检查循环依赖、缺失依赖、未知Tool和非法参数。
- 计算DAG拓扑序和可并行Step集合。
- 验证写Step是否具备补偿或明确的不可补偿说明。

### policy_check

- 结合用户、租户、Scope、Tool风险和参数风险形成决策。
- 输出`ALLOW`、`DENY`或`REQUIRE_APPROVAL`。

### execute_ready_steps

- 并行执行依赖满足的READ Step。
- WRITE和ADMIN Step默认串行。
- 执行前检查deadline、取消状态和幂等记录。

### verify_results

- 使用结构化成功条件检查结果。
- 区分“Tool调用成功”和“业务目标成功”。
- 证据不足时进入受限replan或人工处理。

## 5. PlanStep契约

```json
{
  "step_id": "check_recent_changes",
  "description": "查询发往上海的可用物流供应商",
  "depends_on": [],
  "tool_name": "querySuppliersByDeliveryRegion",
  "arguments": {
    "region": "上海"
  },
  "argument_sources": {
    "region": "user_query"
  },
  "risk_level": "READ",
  "required_scope": "supplier:read",
  "requires_approval": false,
  "timeout_s": 10,
  "max_retries": 2,
  "idempotency_key": null,
  "expected_output_schema": "ChangeList",
  "success_condition": "response.status == 'ok' and len(response.suppliers) > 0",
  "fallback": "ask_for_alternate_delivery_region"
}
```

禁止Planner直接生成任意URL、Shell命令或未注册Tool名称。

## 6. 并行与依赖规则

- 只有`risk_level=READ`且所有依赖完成的Step可以并行。
- 两个会修改同一资源的Step不得并行。
- 写Step的验证Step必须依赖写Step完成。
- 订单创建等后续写动作必须依赖库存和供应商校验成功。
- 并行结果写回状态时使用Step ID合并，不能依赖完成顺序。

## 7. Checkpoint与恢复

Checkpoint应保存：

- 当前Run版本和状态。
- 已完成Step及结果引用。
- 正在执行或可重试的Step。
- 审批状态。
- 幂等键和Tool调用记录。
- Working Memory和上下文摘要。

恢复流程：

1. Worker获得Run执行锁。
2. 加载最新Checkpoint和事件。
3. 核对数据库中的成功Step和幂等记录。
4. 将状态不明确的写Step标记为需要对账，而不是直接重试。
5. 从下一个合法节点继续。

## 8. Memory与上下文预算

| 类型 | 内容 | 生命周期 |
|---|---|---|
| Working Memory | 当前Plan、工具结果摘要和待解决问题 | 当前Run |
| Conversation Summary | 压缩后的对话历史及原消息引用 | 当前会话 |
| Retrieval Memory | 按当前Query动态召回的知识 | 当前节点 |
| Long-term Memory | 显式允许的用户偏好 | 默认关闭 |

Token预算按系统指令、Tool Schema、Plan、知识、历史和输出分别预留。达到阈值后先压缩Tool结果和历史，不删除安全策略、审批信息和引用标识。

## 9. 错误分类

- `VALIDATION_ERROR`：Plan、Schema或参数错误。
- `AUTHORIZATION_ERROR`：无Scope、越权或审批无效。
- `TRANSIENT_TOOL_ERROR`：超时、连接重置、429、部分5xx。
- `PERMANENT_TOOL_ERROR`：4xx业务错误或明确不可恢复错误。
- `LLM_OUTPUT_ERROR`：结构化输出解析失败。
- `DEADLINE_EXCEEDED`：Run超过截止时间。
- `RECOVERY_RECONCILIATION_REQUIRED`：写Step状态不确定，需要对账。

错误分类决定是否重试，禁止对所有异常统一重试。
