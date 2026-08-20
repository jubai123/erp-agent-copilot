# 评测与可观测性：ERP Agent Copilot V6

> 最终交付文档。反映**当前已实现**的评测框架、数据集、指标与可观测性组件；实测数值见 `docs/benchmark.md`。详细设计见 `docs/08-evaluation-observability.md`、`docs/09-implementation-roadmap.md`。

## 1. 评测框架（`evals/`）

Runner 无关的编排框架 `evals/harness.py`：加载五类数据集 → 注入各分类 runner → 汇总 per-category + overall 分数与失败明细。runner 只返回规范化的 `RunnerOutput`，框架不关心分类如何评分。

```text
evals/
├── harness.py            # 加载数据集、run_category、overall_score、失败日志、报告
├── run_all.py            # 五个分类 runner 装配 + 一键入口
├── record_replay.py      # LLM Record/Replay（确定性回归）
├── datasets/             # 10 个数据集（五类离线 175 条接入 run_all；另有 recovery/failure/rule_follow 供单测消费、agent_routing 路由评测、planning_200 分层 LLM 评测）
├── scripts/              # run_security_eval.py / run_ablation.py / analyze_ablation.py / diagnose_keyword_search.py
└── scorers/              # retrieval_scorer.py / rule_scorer.py
```

一键运行：

```bash
uv run evals/run_all.py
uv run evals/run_all.py --report evals/reports/report.json
```

## 2. 数据集与 Runner 来源（诚实标注）

`harness.py` 的 `DATASET_SPECS` 装配 5 个分类 = **175 条**（旧 `recovery_25.json` / `failure_20.json` 已退出评测、保留供 `decide_recovery_action` / `decide_failure_behavior` 单测消费；`rule_follow_20.json` 未接入 run_all，由 rule_scorer 单独使用）。每个分类的 runner **标注来源**，一个数字绝不会被误认作它本不是的东西：

| 分类 | 文件 | 条数 | Runner 模式 | 说明 |
|---|---|---|---|---|
| tool_retrieval | tool_retrieval_40.json | 40 | `deterministic` | **真实生产逻辑**：`candidate_filter` 的 `DOMAIN_TOOL_MAP` 一级过滤，离线可复现 |
| security | security_25.json | 25 | `deterministic` | **真实安全守卫**（注入/SSRF/Redaction），SSRF 用伪造 DNS，逐位复现 |
| knowledge_rag | knowledge_rag_40.json | 40 | `retrieval_pipeline` | **真实检索链路**：embed→vector→FTS→RRF→rerank，对专用测试库、确定性 provider，离线可复现 |
| planning | planning_50.json | 50 | `deterministic` | **真实生产逻辑**：`classify_intent → build_plan_from_intent` 9 工具 DAG 派发，离线可复现 |
| recover_or_replan | recover_or_replan_20.json | 20 | `deterministic` | **真实恢复汇点节点**：`src/erp_copilot/agent/nodes/recover_or_replan.py`（hard-wired 在 `build_agent_graph`），seed 映射成 AgentState 快照后直调，离线可复现 |

> **结论**：五个分类全部跑真实逻辑——四类 `deterministic`（确定性生产代码）+ 一类 `retrieval_pipeline`（真实检索管线，需本地 pgvector 测试库）。**不再有 golden baseline**；每个分数都代表被测系统的真实能力，可逐位复现。

### Runner 模式说明

- **`deterministic`**：调用真实生产代码，无 LLM / 无网络 / 无数据库，位级可复现。
- **`retrieval_pipeline`**：调用真实 RAG 检索链路（embed→vector→FTS→RRF→rerank），确定性 provider 保持离线；需要一个本地 pgvector 测试库，知识库从 datasets/knowledge 幂等灌入。

## 3. 评测指标

### 3.1 检索质量（`evals/scorers/retrieval_scorer.py`）

离线检索指标，`docs/benchmark.md` 中的 42 条查询消融即用此计算：

| 指标 | 含义 |
|---|---|
| Recall@k | 前 k 条结果中包含相关文档的比例 |
| MRR | 首个相关文档倒数排名的均值 |
| NDCG@k | 按相关性折扣的归一化累计增益 |
| Precision@k | 前 k 条结果中相关文档的比例 |

### 3.2 安全守卫（`evals/scripts/run_security_eval.py`）

对 25 条安全用例逐条跑真实守卫（注入 / SSRF / Redaction，SSRF 伪造 DNS）：

```bash
uv run python evals/scripts/run_security_eval.py
uv run python evals/scripts/run_security_eval.py --report report.json
```

| 指标 | 含义 |
|---|---|
| 攻击拦截率 (interception rate) | 被拦截的攻击用例 / 全部攻击用例 |
| 误报率 (false positive rate) | 被拦截的正常用例 / 全部正常用例 |

### 3.3 消融（`evals/scripts/run_ablation.py`）

对同一 42 条查询评测三种检索配置的差异（Vector-only / Hybrid / Hybrid+Rerank），输出 `evals/reports/` 下的 JSON 报告。

### 3.4 分层 LLM 评测与漂移基线（`evals/run_planner_eval.py`）

阶段七（任务 7.10）把 planner 评测从 50 条扩到 **200 条**（`planning_200.json`），按 单步/多步 × easy/hard 分四层各 50（每层 ≥30 保证置信区间有意义）；报告输出各层 score ± 95% Wilson CI，并与 `report_llm_planner_deepseek_real_20260818.json` 实测基线在**共享 case 子集**上计算漂移 delta（`tool_set_exact` 跌 >0.02 告警）。这是"模型/供应商升级后必须重跑同一评测集对比"的回归基线——session 54 的 Prompt 优化已证明该流程有效（contract_valid 86%→94%）。

```bash
uv run python evals/run_planner_eval.py
```

## 4. LLM Record/Replay（`evals/record_replay.py`）

包裹注入的 `llm_complete` 接缝（prompt → completion），一次真实调用后离线重放：

- **record**：调真实 provider，缓存 (prompt, completion)，`save()` 写 JSONL。
- **replay**：按 prompt 精确匹配提供录制结果；未命中抛 `ReplayMissError`，让 CI 中的过期录制**响亮失败**（绝不静默调 provider）。
- **off**：原样返回被包裹的 callable。

这保证了"无 LLM 的确定性回归"：同一组 prompt 在 CI 里重放出的完成内容与录制时逐字节一致。

## 5. 可观测性（`src/erp_copilot/observability/`）

| 组件 | 文件 | 作用 |
|---|---|---|
| 结构化日志 | `logging.py` | JSON 格式化，TraceContext（run_id/step_id/request_id）经 contextvars 贯穿 |
| 分布式追踪 | `tracing.py` | `node_span()` 装饰器、W3C tracecontext 传播 |
| 指标 | `metrics.py` | **12 族**：run 生命周期（created/completed/failed，含 tier 标签）、恢复/审批/采纳率/groundedness/对账计数器、`phase_latency` 直方图、`worker_queue`，经 `/metrics` 输出 Prometheus 文本格式（家族清单见 docs/14） |
| LLM 调用采集 | `langfuse.py` | 采集模型、token、耗时与估算成本，经官方 Langfuse SDK 上报 generation observation（未配置 LANGFUSE_* 时降级不采集，fail-open） |

## 6. 评测与可观测的关系

- **评测回答"系统做对没有"**：确定性数据集 → 可复现分数 → 消融定位哪一环（向量 / 关键词 / Rerank）贡献了多少。
- **可观测性回答"线上发生什么"**：Trace 还原一次 Run 的每一步，指标看吞吐与延迟，日志看错误上下文。
- 二者共用同一接缝（`llm_complete` 注入、`node_span` 装饰、事件表）：评测时可以离线重放，观测时采集真实调用。

## 7. 复现与验证命令

```bash
uv run evals/run_all.py                 # 175 条五大分类评测
uv run python evals/scripts/run_security_eval.py   # 25 条安全守卫评测
uv run python evals/scripts/run_ablation.py        # 42 条检索消融
uv run pytest tests/ -q                 # 单元测试（1691 条通过）
uv run python tests/performance/fault_injection.py # 故障注入 3/3 PASS
```
