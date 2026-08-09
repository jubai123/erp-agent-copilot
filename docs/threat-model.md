# 威胁模型与安全防护：ERP Agent Copilot V6

> 最终交付文档。反映**当前已实现**的五层防护与其对应的代码；详细设计见 `docs/06-security-approval-and-recovery.md`、`docs/07-security-approval-and-recovery.md`。

## 1. 资产与信任边界

| 资产 | 位置 | 保护目标 |
|---|---|---|
| 租户业务数据（订单/库存/供应商） | PostgreSQL + ERP Simulator | 完整性、租户隔离、防篡改 |
| 审批决策 | `audit_logs` + checkpoint | 可审计、不可抵赖、只允许授权审批人 |
| 知识库文档 | `knowledge_documents` / `document_chunks` | 始终作为**数据**而非指令（防 RAG 投毒） |
| 密钥 / PII | 各类输出 | 不进入日志、评测集或仓库 |
| 对外请求 | Agent → ERP/MCP | 只允许预注册的出口（防 SSRF） |

信任边界：模型输出与检索知识**永不可信**，都要经过守卫；审批人身份必须来自租户内授权，租户边界取自 `runs` 行而非客户端。

## 2. 五层安全防护（输入 → 输出）

| 层 | 组件 | 代码 | 拦截什么 |
|---|---|---|---|
| 1. 输入层 | Prompt Injection 守卫 | [injection_guard.py](../src/erp_copilot/security/injection_guard.py) | "忽略指令/跳过审批/泄露系统提示词"注入族 |
| 2. 出口层 | SSRF 守卫 | [ssrf_guard.py](../src/erp_copilot/security/ssrf_guard.py) | 非白名单主机、内网/环回/私有地址、DNS rebinding |
| 3. 权限层 | Policy 门控 | [policy_check.py](../src/erp_copilot/agent/nodes/policy_check.py) | 缺 Scope 的步骤直接 DENY，审批不能授予权限 |
| 4. 审批层 | 写操作人工审批 | [approval.py](../src/erp_copilot/security/approval.py) | WRITE/DANGEROUS 步骤在 `WAITING_APPROVAL` 暂停 |
| 5. 输出层 | 密钥/PII Redaction | [redaction.py](../src/erp_copilot/security/redaction.py) | API Key、手机号、身份证号脱敏 |

> **确定性优先**：所有守卫都是纯函数（注入 resolver/时钟/scope 解析器），无网络、无随机、无 LLM，行为可审计、可逐位复现（安全评测即据此建立，见 `docs/evaluation.md` §3.2）。

## 3. 威胁与缓解对照

### T1 提示注入 / RAG 投毒

- **场景**：用户输入或检索到的知识文本携带"忽略前置指令/跳过审批/泄露系统提示词"。
- **缓解**：`InjectionGuard.check()` 正则匹配三类注入族；判定由调用方决定处置——输入层 `blocked`、知识层 `quarantined`（[injection_guard.py:95](../src/erp_copilot/security/injection_guard.py#L95)）。拦截写 `security_events`。
- **兜底**：即使注入文本进了模型，`policy_check` 的 Scope 门 + 审批门仍会拦写操作——业务文档永远是**数据**，覆盖不了系统策略。

### T2 SSRF / 出口滥用

- **场景**：模型或工具调用了内网地址、本机回环、DNS rebinding 到的私网 IP。
- **缓解**：`SSRFGuard.check()` 顺序校验——scheme 白名单 → 拒绝 URL 内嵌凭据 → 端口白名单 → 主机白名单 → 解析后逐地址检查内网/环回/链路本地/组播/保留段（[ssrf_guard.py:127](../src/erp_copilot/security/ssrf_guard.py#L127)）。重定向只允许同主机并逐跳复查。
- **已知局限**（诚实标注）：`check` 之后连接再自行解析存在 TOCTOU——守卫与执行器不共用同一连接。彻底关闭需执行器直连已校验地址，超出当前范围（[ssrf_guard.py:17](../src/erp_copilot/security/ssrf_guard.py#L17)）。

### T3 越权 / 租户边界破坏

- **场景**：用户操作了非本租户的数据或未授权 Scope 的步骤。
- **缓解**：`decide_step()` 先跑 Scope 门——`required_scope` 不在用户 Scope 集合即 `DENY`，与风险级别无关，**审批只能放行执行、不能授予权限**（[policy_check.py:27](../src/erp_copilot/agent/nodes/policy_check.py#L27)）；`user_id=None` 解析为空 Scope（安全默认）。租户边界取自 `runs` 行而非客户端（[approve_run](../apps/api/routes/runs.py)）。

### T4 审批被绕过 / 决策可抵赖

- **场景**：未审批写步骤被执行，或审批后无法追责。
- **缓解**：`request_approval` 节点把 WRITE/DANGEROUS 步骤置 `WAITING_APPROVAL`；`ApprovalDecisionService.decide()` 只允许把 PENDING 记录翻转为 APPROVED/DENIED（重复决策报 `APPROVAL_ALREADY_DECIDED`）；决策先写 checkpoint、再写 `audit_logs`（`created_at` 不可 onupdate、立即提交，即使后续 resume 失败也已审计），最后才 resume 图（[approval.py:165](../src/erp_copilot/security/approval.py#L165)）。

### T5 密钥/PII 泄露

- **场景**：模型回答携带 API Key、手机号、身份证号。
- **缓解**：`Redactor.redact()` 逐条规则原地改写——密钥变 `[REDACTED]`，手机号/身份证保边掩码（`138****8000`、前 6 后 4），审计仍可核对但不泄露全文（[redaction.py:76](../src/erp_copilot/security/redaction.py#L76)）。

### T6 重复副作用 / 崩溃不一致

- **场景**：消息重投、Worker 崩溃导致同一次写被执行两次，或状态未知。
- **缓解**：`IdempotencyStore` at-most-once（PENDING → 执行 → COMPLETED，`uq_idempotency_tenant_key` 唯一约束）；`RunRecovery.load` 对账幂等记录——PENDING 写步骤标 `RECOVERY_RECONCILIATION_REQUIRED`，COMPLETED 永不重跑。见 `docs/benchmark.md` §4 故障注入验证。

## 4. 安全事件与审计

- `security_events`：注入 / SSRF 拦截事件（`attack_type / layer / severity / disposition / input_summary`）。
- `audit_logs`：审批等敏感操作（`actor / resource / action / result / ip / trace_id`），`created_at` 只写不改。
- 所有拦截 / 恢复 / 降级都写事件，供安全评测与事后分析（`docs/08 §9`）。

## 5. 安全评测

25 条安全用例（`evals/datasets/security_25.json`）对真实守卫（注入 / SSRF / Redaction，SSRF 伪造 DNS）逐条评测，输出**攻击拦截率**与**误报率**：

```bash
uv run python evals/scripts/run_security_eval.py
```

## 6. 剩余风险（诚实标注）

| 风险 | 状态 |
|---|---|
| `/v1/knowledge/search` 检索管线未接入 HTTP 层（STUB），知识层 quarantine 处置点尚未触发 | 待集成 |
| Celery worker 未接入完整 LangGraph 图，`policy_check`/`request_approval` 在 worker 执行链路上未生效 | 待集成 |
| SSRF 的 TOCTOU 间隙（守卫与连接各自解析） | 已知局限 |
| 注入守卫是"窄检测器"：识别经典三族，不做通用恶意意图分类 | 设计取舍 |
