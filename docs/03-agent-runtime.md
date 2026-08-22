# Agent Runtime设计

> 架构决策摘要见 [11-architecture-decisions.md](11-architecture-decisions.md)。本文件对应其中决策一（固定管道检索）、决策四（Plan-then-Execute）、决策五（工具候选过滤）与决策六（四层防御）。

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

- **必走节点**：每个 Run 都会执行，LLM 不决策"是否检索"。原因：LLM 不知道自己不知道什么，自主决定会纵容幻觉编造流程。
- 检索L2知识（ERP业务规则、API说明、字段语义、订单流程和异常处理文档）。
- 对检索结果执行租户、ACL和可信度过滤。
- 保存引用位置和检索分数。
- 仅在 Phase 4 评测证明"验证时知识不足需要再查一轮"时，才从 `verify_results` 加条件边回退到本节点。默认不启用。

### build_plan

- 只输出符合Schema的Plan DAG。
- 不能调用Tool，也不能绕过Policy。
- 每个参数必须记录来源或待确认状态。
- **输入被约束**：收到的工具候选是意图过滤后的 3-8 个（见 决策五），不是全部工具；收到的知识是 L1 硬约束规则加 L2 检索结果，L1 优先。
- **L1/L2 注入顺序**：System → L1 硬约束规则 → L2 Retrieved Knowledge（参考）→ Available Tools（候选）→ User Query。

**三层漏斗（词表外默认开 LLM）**：`classify_intent` 先判复杂度/区域/词表，Tier1 词表内查询走确定性 `build_plan` 快速路径；Tier2/Tier3 词表外查询（复杂度闸门、未知区域、EMPTY_PLAN）走 LLM 规划节点 `build_plan_llm`。LLM 漏斗**默认启用**（`LLM_PLANNING_ENABLED=true`，见 .env.example），词表外意图/规划真正进生产主路径；`resolve_llm_plan_node` 三态接线：`false` 保持离线（tier2/3 诚实失败 `ROUTED_TIER23_NO_LLM`）、启用但无 key 报配置错误 `LLM_NOT_CONFIGURED`、启用有 key 走真实 `build_plan_node`。LLM 输出仍被 §5.6 四层确定性护栏严格校验，路由边界（`route_query_layer`）不随开关变化。

**LLM 兜底上限（实测，2026-08-22 真实 DeepSeek held-out 冻结集 40 例）**：词表外走 LLM 时工具选型准确率 **0.95**（3 次重复 0.925/0.950/0.975，单步 1.0 / 多步 0.90）、parse 1.0、工具覆盖 0.99；5 个失败里 3 个"工具选对了、计划没通过确定性校验"（`BROKEN_ARGUMENT_SOURCE`）、2 个工具选错（create 域供应商选型：plan-161 多选、plan-191 误选）。早期的"订单写操作漏查 `getOrderByOrderId`"失败已通过 SYSTEM_PROMPT"写前必查"固定规则修掉：dev 160 例 A/B 显示订单写子集选型 29/33→33/33、全局无回归，held-out 3 次重复不再出现。结论：LLM 的贡献被封顶在 ~95% 选型，工具对但参数来源不对的计划被护栏拦下，无法静默污染执行；与更易的 50 例基线在共享 12 例上 drift 0.0（难度差异而非模型退化）。报告只报 held-out 冻结集数字（`--split heldout`，默认），prompt 调优只能在 dev 补集上做、held-out 上显式 `--system` 被 CLI 拒绝。

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

## 5.5 工具候选过滤

`build_plan` 不面向全部工具做"大海捞针"，而是两层过滤后的小候选集：

```text
第一级：意图→域确定性过滤（主引擎，必过）
  classify_intent 输出 (domain, action)
    → DOMAIN_TOOL_MAP 精确映射
    → 候选 3-8 个，直接进 build_plan Prompt

第二级：向量检索精排（按需，仅当候选 > 阈值时启用）
  候选数超过阈值（如 5-8 个）或描述超过 Token 预算
    → 向量粗筛 + Rerank 精排 → Top-N
```

V6 当前 9 个工具，第一级过滤后候选 3-5 个，直接进 Prompt，第二级不启用。架构上预留第二级接口，工具数量增长到每域超过 5-8 个时激活。这是 V5 两阶段（Milvus 向量 + Rerank）在 V6 中的重定义：从"每次必走的唯一引擎"降级为"意图过滤后的按需精排层"。详见 [11-architecture-decisions.md](11-architecture-decisions.md) 决策五。

## 5.6 Plan 正确性的四层防御

Plan DAG 的正确性由四层叠加保证，单层无法防语义错选：

| 层 | 拦截什么 | 是否确定性 |
|---|---|---|
| 1. 约束候选空间（上游） | 缩小 LLM 犯错面：从 25 选 1 变成 3 选 1 | 是 |
| 2. `validate_plan` | 未知Tool、Schema不匹配、缺失依赖、DAG环、写Step无补偿 | 是 |
| 3. MCP Gateway | Scope、租户隔离、SSRF、参数类型转换 | 是 |
| 4. `verify_results` | `success_condition` 匹配返回，防"工具调用成功但业务错" | 是 |

例如"查苹果却选了 getProductById 传 id=苹果"：第2层放行（Schema 都接受一个参数），但第4层 `success_condition` 检查返回产品名是否为"苹果"，验证失败。因此既要靠第4层兜底，也要靠第1层把语义错选概率压到评测可接受范围。

**实证（真实 DeepSeek held-out 冻结集 40 例）**：四层中真正拦下 LLM 错误的是第2层 `validate_plan`——5 个失败里 3 个是工具选对但计划没过确定性校验（`BROKEN_ARGUMENT_SOURCE`），第1层选型上限 95%（单步 100%、多步 90%，3 次重复 0.925-0.975）之外是 2 例工具选错（create 域供应商选型）。早期还有 3 例"订单写操作漏查 `getOrderByOrderId`"，已通过 SYSTEM_PROMPT"写前必查"固定规则修掉（dev A/B + held-out 3 次重复验证）。护栏不追求"LLM 零错误"，而是把 LLM 的贡献封顶在可测区间、把剩余错误变成确定性拦截而非静默放行。

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
- `WRITE_OUTCOME_AMBIGUOUS`：写Step以歧义性瞬态失败（TIMEOUT/UPSTREAM_5xx/UPSTREAM_UNAVAILABLE），云端可能已生效且无服务器端幂等，禁止自动重试或重规划，需人工对账。

错误分类决定是否重试，禁止对所有异常统一重试。
