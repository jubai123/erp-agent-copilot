# 最终交付总结：ERP Agent Copilot V6

> 收官文档（2026-08-09 首版，2026-08-20 阶段七收尾后更新）。本页只写**当前真实状态**：已实现的能力、实测数字、未完成项与诚实边界。所有数字可复现（复现命令见 [benchmark.md](benchmark.md)），不把"目标"写成"已实现"。
>
> 配套：产品与架构见 [product.md](product.md) / [architecture.md](architecture.md)；API 契约见 [api-contracts.md](api-contracts.md)；威胁模型见 [threat-model.md](threat-model.md)；评测见 [evaluation.md](evaluation.md)；工程化指标计划见 [14-engineering-metrics-plan.md](14-engineering-metrics-plan.md)；[REDACTED]材料见 [12-resume-and-pitch.md](12-resume-and-pitch.md)。

## 1. 一句话交付

把自然语言 ERP 业务需求编译为**可解释、可审批、可恢复**的跨系统工具调用 DAG 的 Agent Runtime：FastAPI + LangGraph 状态机 + Celery + PostgreSQL/pgvector 混合检索 + MCP，四个进程、两个基础设施，**1691 个单元测试**、175 条离线评测、故障注入 3/3，并接通 AI 生产指标闭环（Trace/LLM 接线、恢复/审批/采纳率、Tier 分层成功率、在线 groundedness、写路径对账）。

## 2. 交付范围（已实现）

**进程与基础设施**：API :8000、ERP Simulator :8001、Agent Worker（Celery）、MCP Gateway :8002 四个进程 + Postgres/pgvector :5432、Redis :6379。

**按层的关键能力**：

| 层 | 已实现模块 |
|---|---|
| 工具层 | OpenAPI 3.x 导入校验、Tool/ToolVersion 版本化、风险分级（read/write/dangerous）、候选过滤（`candidate_filter` 9 工具确定性分类学）、MCP Gateway、统一 `ToolResult`/`ToolError` |
| 检索层 | L1 Skill 确定性注入（`skill_matcher`，意图→技能映射 42/42 命中）+ L2 RAG（`ingestion`→pgvector 向量 + PostgreSQL FTS 混合→RRF→Cross-Encoder Rerank）+ 引用与无依据拒答；**生产配置 = Hybrid+Rerank**（消融定案，ADR 决策七） |
| Agent 层 | 强类型 `AgentState` + 12 节点 LangGraph：确定性意图分类、三层漏斗路由（Tier1 确定性 / Tier2 LLM 约束 / Tier3 自由规划，**词表外查询默认走 LLM**，`LLM_PLANNING_ENABLED=false` 切回离线诚实失败 `ROUTED_TIER23_NO_LLM`）、Plan DAG 校验（Kahn 拓扑 + 波动分组：READ 并行 / WRITE 串行）、Policy Scope 门、写审批分支、语义验证门（AST 白名单安全 eval）、Checkpoint 恢复、recover_or_replan 图内恢复汇点 |
| 安全层 | 五层防护：注入守卫 / SSRF 守卫 / Policy 门控 / 审批 / 输出脱敏；幂等键 at-most-once；退避重试；失败队列；安全事件与审计 |
| 可观测层 | 结构化日志、OpenTelemetry Trace（`node_span` 生产接线 7 节点）、Prometheus 指标（**12 族**）、Langfuse、Record/Replay 评测；**AI 生产指标闭环**（阶段七，见 §2.1） |
| 评测层 | 175 条五类离线评测 + 42 条检索消融 + 25 条安全守卫 + 故障注入 3/3 + **分层 200 例 LLM 评测与漂移基线** + Locust 负载（方法就绪） |
| 工程化 | uv 环境、ruff/mypy 门禁、pre-commit、Alembic 迁移（12 版本）、CI（lint/type/test + PG/Redis 服务容器）、`v1.0.0` 标签、统一工程化指标评测 `run_engineering_metrics.py`（overall **PASS**） |

### 2.1 阶段七：AI 生产指标闭环（7.1–7.11，全部完成）

| 任务 | 能力 | 落点 |
|---|---|---|
| 7.1 | worker 图 7 节点统一挂 `node_span`（Trace 生产接线，async-aware） | `apps/worker/graph_builder.py` |
| 7.2 | LLM 调用点接 `llm_call`（token 回填经生产走线） | LLM 调用封装 |
| 7.3 | 恢复计数器 `erp_run_retries_total` / `_replans_total` / `_abandoned_total` | recover_or_replan 三分支 |
| 7.4 | 审批介入率 `erp_approval_requests_total{outcome=pending/approved/denied}` | request_approval |
| 7.5 | phase_latency 改 `perf_counter` 测量（修复 Win11 15.6ms 粒度 0ms 假象） | `_observe_phase` |
| 7.6 | run 完成/失败加 `tier` 标签 + 错误分类学聚合 `evals/scripts/error_taxonomy.py` | persist_run / 崩溃分支 |
| 7.7 | token/成本/单 Run 调用次数聚合 `evals/scripts/llm_usage_report.py`（按价表重估） | LLM_CALL 日志 |
| 7.8 | 计划采纳率 `erp_plan_outcome_total{outcome=accepted/edited/rejected}`（区分原样/修改接受） | ApprovalDecisionService + approve API |
| 7.9 | 在线 groundedness/拒答率 `erp_answer_grounded_total{grounded=yes/no/refused}`（`classify_answer_groundedness` 按引用覆盖打标，None-skip） | `agent/groundedness.py` + persist_run 出站埋点 |
| 7.10 | LLM 评测扩到 **200 例分层**（single/multi × easy/hard 各 50）+ 每层 95% Wilson CI + 与 DeepSeek 基线漂移检测 | `evals/datasets/planning_200.json` + `llm_planner_eval.py` |
| 7.11 | 写路径端到端闭环：审批 → 幂等 → 对账；`erp_reconciliation_success_total{outcome=consistent/mismatch}`，终态统一经 `persist_run` 注入 reconciler | `application/reconciliation.py` + 双终态路径 |

## 3. 任务完成度（87 项计划 → 87 项全部完成）

七个阶段、87 项任务；**87 项全部完成**。

| 阶段 | 任务数 | 状态 |
|---|---|---|
| 一：工程骨架 | 12 | ✅ 全部完成（uv/pyproject、领域模型、App 工厂、Celery、CI、Alembic） |
| 二：工具与模拟器 | 12 | ✅ 全部完成（Tool Registry、OpenAPI 导入、ERP Simulator 商品/供应商/订单/场景、导入与 Run API、e2e） |
| 三：知识检索 | 14 | ✅ 完成（统一事实表、L1 Skill、知识库文档、导入分块、混合检索、Rerank、引用、评测集、消融定案） |
| 四：Agent Runtime | 16 | ✅ 全部完成（4.13 上下文预算 / 4.14 SSE / 4.15 取消超时 / 4.16 图端到端演示 / 4.17 写意图进生产图） |
| 五：安全审批恢复 | 10 | ✅ 全部完成（RBAC Scope、审批、SSRF、注入守卫、Redaction、幂等、退避重试、恢复、失败队列、安全评测） |
| 六：评测与交付 | 12 | ✅ 全部完成（可观测、175 条评测、Harness、Record/Replay、Locust、故障注入、文档、清理 Release、[REDACTED]讲稿） |
| 七：AI 生产指标与可观测性接线 | 11 | ✅ 全部完成（2026-08-19/20，见 §2.1；`engineering_metrics.json` overall **PASS**） |

## 4. 实测数字（全部可复现）

| 指标 | 结果 | 依据 |
|---|---|---|
| 单元测试 | **1691 通过** | `uv run pytest tests/ -q` |
| 类型检查 | 103 个 src+apps 文件 mypy 干净 | `uv run mypy src/ apps/` |
| 静态检查 | ruff check / format 全绿 | `uv run ruff check .` / `uv run ruff format --check .` |
| 检索消融（42 条） | Rerank 使 **Recall@5 0.7381→0.9167**、NDCG@5 0.6824→0.8249；代价 P50 ~53→~226ms；Vector-only Recall@1 0.3968 反最高 | `uv run python evals/scripts/run_ablation.py` |
| 故障注入 | **3/3 PASS**（崩溃恢复 / 超时重试 / 订单幂等；负例证明 harness 不虚绿） | `uv run python tests/performance/fault_injection.py` |
| 安全评测 | 25 条：**拦截率 100%（20/20）、误报率 0%（0/5）**，确定性守卫伪 DNS | `uv run python evals/scripts/run_security_eval.py` |
| 评测集 | 175 条 / 5 类（40+40+50+20+25）；五分类全部跑真实逻辑（tool_retrieval/planning/recover_or_replan/security 为 deterministic，knowledge_rag 为 retrieval_pipeline） | `uv run evals/run_all.py` |
| LLM 评测 | **200 例分层** planning 评测，各层 score ± 95% Wilson CI，与 DeepSeek 实测基线对比漂移 | `uv run python evals/run_planner_eval.py` |
| 工程化指标 | `run_engineering_metrics.py` **overall PASS**（D1-D4 四组全 PASS，gaps 0；coverage 96.14%，metric_families 12/12，retry_rate 4.0 / recovery_rate 1.5 MEASURED） | `uv run python evals/run_engineering_metrics.py` |
| 代码规模 | src 71 .py + apps 36 .py + tests 126 .py；git 418 跟踪文件；tag `v1.2` | `git ls-files \| wc -l` |
| 负载测试 | 50 并发 / 60s：**1385 请求 0 失败，吞吐 23.25 req/s，P50 20ms / P95 44ms / P99 57ms**（2026-08-11 本机实测，测试库） | `tests/performance/locustfile.py` |

## 5. 未完成项与诚实边界

### 5.1 曾列为未完成的任务（v1.1 已全部完成）

| 任务 | 落地 | 影响消除 |
|---|---|---|
| 4.13 上下文预算 | `memory/context_budget.py` | LLM 输入 token 有预算上限 |
| 4.14 SSE 事件流 | `GET /v1/runs/{id}/events` | 客户端实时收到事件流 |
| 4.15 取消/超时 | `POST /v1/runs/{id}/cancel` + check_deadline/EXPIRED | 长任务可取消、有超时兜底 |
| 4.16 图端到端演示 | Worker `execute_run` 接入完整 LangGraph | 生产链路走完整图行为 |
| 4.17 写意图进生产图 | `graph_builder.py` 写意图 `createOrder`/`cancelOrder`/`updateOrderStatus` 进入生产图 | 写路径端到端（7.11）的 DAG 前置 |

### 5.2 接线现状

- `POST /v1/knowledge/search` 已接入完整检索管线（embed → 向量 → 关键词 → RRF → rerank），不再是 STUB。
- Celery worker 的 `execute_run` 由 `build_agent_graph(checkpoint_saver=...)` 驱动：确定性 planner → validate → policy → ERP 模拟器 executor → verify 全真实节点，Checkpoint 绑定 DB 会话，`policy_check` / `request_approval` / `recover_or_replan` 在 worker 链路生效。
- 阶段七生产接线全部真实：worker 图 7 节点 `node_span`、LLM 调用点 `llm_call`、`persist_run` 出站 groundedness 埋点、worker 与 API 审批续跑双终态统一经 `persist_run` 注入写路径 reconciler。
- `evals/run_all.py` 五分类全部跑真实逻辑（`tool_retrieval`/`planning`/`recover_or_replan`/`security` 为 `deterministic`，`knowledge_rag` 为 `retrieval_pipeline`）；其中 `knowledge_rag` 需要本地 pgvector 测试库，离线单测中该分类注入 fake runner。

### 5.3 已知技术局限（威胁模型剩余风险）

- SSRF 守卫与连接各自解析存在 **TOCTOU** 间隙（守卫后执行器需直连已校验地址才能彻底关闭）。
- 注入守卫是"窄检测器"：识别经典三族，不做通用恶意意图分类（设计取舍）。
- groundedness 是**词法代理**而非语义裁决：`yes` 表示答案值字面出现在引用中，未用 NLI 模型判定蕴含（诚实标注，离线可复现优先）。

## 6. 仓库与发布状态

- **git**：分支 `main`（默认分支）；`v1.0.0` 注解标签指向 2026-08-09 的 `0954377`（阶段六收尾）；**`v1.2` 注解标签指向当前 HEAD（2026-08-20，阶段七 AI 生产指标闭环 / 87 任务全部完成）**；工作树干净。
- **远端**：`origin` → `https://github.com/jubai123/erp-agent-copilot.git`，本地领先 `origin/main` 21+ 提交未推送。
- **门禁**：`uv run pytest tests/ -q` **1691 通过**、`uv run mypy src/ apps/` 干净（103 文件）、`uv run ruff check` / `format` 全绿。

## 7. 复现全部数字

```bash
uv run pytest tests/ -q                       # 1691 通过
uv run mypy src/ apps/                            # 103 文件干净
uv run ruff check . && uv run ruff format --check
uv run python tests/performance/fault_injection.py   # 3/3 PASS
uv run python evals/scripts/run_ablation.py          # 42 查询消融
uv run python evals/scripts/run_security_eval.py     # 25 条守卫
uv run evals/run_all.py                              # 175 条五类评测
uv run python evals/run_planner_eval.py              # 200 例分层 LLM 评测 + 漂移
uv run python evals/run_engineering_metrics.py       # 四维度工程化指标（overall PASS）
```

## 8. v1.1 与阶段七里程碑完成情况

**v1.1 计划项已全部完成**：

1. **把 worker 接入完整 LangGraph 图**（4.16）——`execute_run` 由 `build_agent_graph(checkpoint_saver=...)` 驱动，`policy_check` / `request_approval` / Checkpoint 生效。
2. **补 4.15 取消/超时**——cancel 端点 + deadline 检查 + EXPIRED 流转。
3. **补 4.13 上下文预算**与 **4.14 SSE**——`context_budget` + SSE 事件流。
4. **把 golden_baseline 四类换成真实 pipeline runner**——五分类全为真实逻辑。
5. **跑 Locust 负载测试并回填 P50/P95**——50 并发/60s 实测 1385 请求 0 失败，已回填 docs/benchmark.md §5。
6. **实现 recover_or_replan 图内恢复汇点**——重试/重规划/放弃三路决策，预算有界、at-most-once。

**阶段七（AI 生产指标闭环）已全部完成**：见 §2.1。至此工程化指标评测 `run_engineering_metrics.json` 的 `overall_status` 达到 **PASS**（D1-D4 四组全 PASS，gaps 0；coverage 96.14%，metric_families 12/12，retry_rate 4.0 / recovery_rate 1.5 MEASURED，test_functions_count 1445）。
