# 评测与可观测性：ERP Agent Copilot V6

> 最终交付文档。反映**当前已实现**的评测框架、数据集、指标与可观测性组件；实测数值见 `docs/benchmark.md`。详细设计见 `docs/08-evaluation-observability.md`、`docs/09-implementation-roadmap.md`。

## 1. 评测框架（`evals/`）

Runner 无关的编排框架 `evals/harness.py`：加载六类数据集 → 注入各分类 runner → 汇总 per-category + overall 分数与失败明细。runner 只返回规范化的 `RunnerOutput`，框架不关心分类如何评分。

```text
evals/
├── harness.py            # 加载数据集、run_category、overall_score、失败日志、报告
├── run_all.py            # 六个分类 runner 装配 + 一键入口
├── record_replay.py      # LLM Record/Replay（确定性回归）
├── datasets/             # 7 个数据集，共 220 条
├── scripts/              # run_security_eval.py / run_ablation.py / analyze_ablation.py / diagnose_keyword_search.py
└── scorers/              # retrieval_scorer.py / skill_scorer.py
```

一键运行：

```bash
uv run evals/run_all.py
uv run evals/run_all.py --report evals/reports/report.json
```

## 2. 数据集与 Runner 来源（诚实标注）

`harness.py` 的 `DATASET_SPECS` 装配 6 个分类 = **200 条**（7 个数据文件共 220 条，其中 `skill_follow_20.json` 未接入 run_all，由 skill_scorer 单独使用）。每个分类的 runner **标注来源**，一个数字绝不会被误认作它本不是的东西：

| 分类 | 文件 | 条数 | Runner 模式 | 说明 |
|---|---|---|---|---|
| tool_retrieval | tool_retrieval_40.json | 40 | `deterministic` | **真实生产逻辑**：`candidate_filter` 的 `DOMAIN_TOOL_MAP` 一级过滤，离线可复现 |
| security | security_25.json | 25 | `deterministic` | **真实安全守卫**（注入/SSRF/Redaction），SSRF 用伪造 DNS，逐位复现 |
| knowledge_rag | knowledge_rag_40.json | 40 | `golden_baseline` | 预言机返回 ground-truth 文档（或拒答）；**尚未接入真实检索管线** |
| planning | planning_50.json | 50 | `golden_baseline` | 预言机返回预期步骤序列；**尚未接入真实 Planner** |
| recovery | recovery_25.json | 25 | `golden_baseline` | 预言机返回预期恢复动作；**尚未接入真实恢复流程** |
| failure | failure_20.json | 20 | `golden_baseline` | 预言机返回预期行为并观测零重复写；**尚未接入真实幂等执行** |

> **结论**：`tool_retrieval` 与 `security` 两个分类跑真实逻辑；其余四个分类用 golden baseline 先把 harness 管线端到端跑通——换入真实 runner（检索管线 / LangGraph executor）时无需改动 harness。这正是"先把评测基线立起来、再逐分类替换真实实现"的路线。

### Runner 模式说明

- **`deterministic`**：调用真实生产代码，无 LLM / 无网络 / 无数据库，位级可复现。
- **`golden_baseline`**：直接返回数据集的期望答案。它**不代表被测系统已实现**，只证明评测管线本身正确；真实 runner 就位后该分类的分数才有意义。

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
| 指标 | `metrics.py` | `runs_created/completed/failed`、`phase_latency` 直方图、`worker_queue`，经 `/metrics` 输出 Prometheus 文本格式 |
| LLM 调用采集 | `langfuse.py` | 采集模型、token、耗时与估算成本（自研实现，未引入外部 SDK） |

## 6. 评测与可观测的关系

- **评测回答"系统做对没有"**：确定性数据集 → 可复现分数 → 消融定位哪一环（向量 / 关键词 / Rerank）贡献了多少。
- **可观测性回答"线上发生什么"**：Trace 还原一次 Run 的每一步，指标看吞吐与延迟，日志看错误上下文。
- 二者共用同一接缝（`llm_complete` 注入、`node_span` 装饰、事件表）：评测时可以离线重放，观测时采集真实调用。

## 7. 复现与验证命令

```bash
uv run evals/run_all.py                 # 200 条六大分类评测
uv run python evals/scripts/run_security_eval.py   # 25 条安全守卫评测
uv run python evals/scripts/run_ablation.py        # 42 条检索消融
uv run pytest tests/unit                # 单元测试（874 条通过）
uv run python tests/performance/fault_injection.py # 故障注入 3/3 PASS
```
