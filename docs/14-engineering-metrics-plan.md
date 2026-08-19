# 工程化指标评测计划：ERP Agent Copilot V6

> 评测计划文档。定义本项目"工程化指标"的四维度谱系：每个指标给出 **定义（What）/ 为什么（Why）/ 测量方法（How）/ 验收标准 / 当前基线**，并配套统一评测入口 `evals/run_engineering_metrics.py`。实测数值仍以 `docs/benchmark.md` 为准；本页负责"怎么量、量什么、合格线在哪"。

## 1. 目标与原则

**目标**：让"工程质量"可测量、可复现、可回归。一次运行产出覆盖四个维度的统一报告，任何改动后都能看到指标漂移。

**原则**（与 `docs/README.md` 一致）：

- **诚实优先**：未测的项标注"待实测"，不编造数字；工具缺失标注 `NOT_CONFIGURED`，不虚绿。
- **真实逻辑**：评测必须跑真实生产代码（确定性节点、真实检索链路），不做 golden baseline。
- **离线可复现**：除需测试库的项外，所有指标无网络、无真实 LLM 即可复现。
- **验收标准 = 目标值**：目标值不等于基线值；未达标的项必须 FAIL，而不是模糊带过。

## 2. 指标谱系总览

| 维度 | 覆盖问题 | 子项数 |
|---|---|---|
| **D1 系统质量** | 系统做对了没有？ | 正确性/性能/可靠性/安全 |
| **D2 端到端运行** | 一次真实 Run 表现如何？ | 成功率/阶段延迟/失败路径 |
| **D3 可观测性** | 指标采集本身可靠吗？ | 指标族/追踪/日志 |
| **D4 工程过程** | 代码工程化程度如何？ | 规模/覆盖/复杂度/规范 |

## 3. 维度详解

### D1 系统质量总指标（correctness / performance / reliability / security）

| 指标 | 定义 | 测量方法 | 验收标准 | 当前基线 |
|---|---|---|---|---|
| 评测集正确率 | 五分类 175 条加权通过率 | `run_all(CATEGORY_RUNNERS)` | ≥ 0.95 | 待本次实测 |
| 单测通过 | `pytest tests/` 失败数 | 子进程 | 0 failed | 1691 passed |
| 类型检查 | `mypy src/ apps/` 干净文件数 | 子进程 | 0 error | 103 文件 |
| 静态检查 | `ruff check` / `ruff format --check` | 子进程 | 0 error | 通过 |
| 故障注入 | 三场景 pass/total | `fault_injection.run_all()` | 3/3 PASS | 3/3 |
| 安全拦截率 | 攻击用例被拦截比例 | security_25 分类 | 100% | 100% (20/20) |
| 安全误报率 | 正常用例被误拦比例 | security_25 分类 | 0% | 0% (0/5) |
| 负载吞吐/P95 | locust 50 并发 60s | 手动复现 | 0 fail | 23.25 req/s, P95 44ms |

**教学点**：为什么正确率看加权不简单平均？`overall_score` 按 case 数加权（`harness.overall_score`），否则一个 25 条的分类和 50 条的分类各占 50%，小数据集被夸大。加权 = 每类分数 × 该类 case 数 / 总 case 数。

### D2 端到端 Agent 运行指标（e2e runtime）

| 指标 | 定义 | 测量方法 | 验收标准 | 当前基线 |
|---|---|---|---|---|
| happy_path 成功率 | 真实 Run 达 COMPLETED | TestClient + Celery eager 驱动 | 100% | 单测覆盖 |
| 阶段延迟分解 | plan/execute/verify 均值 | 读 `erp_phase_latency_seconds` 直方图 | 记录 | **已接线**（任务 7.5 改 `perf_counter` 修复 Win11 时钟粒度） |
| 失败路径识别 | timeout 场景正确 FAILED | 驱动 timeout 场景 | 正确 FAILED | 单测覆盖 |
| 执行成功率 | `(happy_ok + timeout_ok) / 2`，场景到达**预期终态**比例（happy→COMPLETED、timeout→FAILED 各记 1 分） | e2e 驱动逐场景判定 | 1.0 | 实测 |
| 重试率 / 恢复率 | retry_count/replan_count 占比 | `erp_run_retries_total`/`erp_run_replans_total`/`erp_run_abandoned_total`（任务 7.3） | MEASURED | retry_rate 4.0 / recovery_rate 1.5 |

**教学点**：为什么阶段延迟从 Prometheus 直方图读而非自己计时？评测脚本与生产埋点用**同一个**采集器（`graph_builder._observe_phase` 写 `METRICS.phase_latency`），测得的就是线上会看到的值；若脚本自己计时，埋点漂移就测不出来。读法用 `generate_latest` 文本解析 `_sum{phase=..}`/`_count{phase=..}`，这是公共 API，不碰私有 `_sum` 字段。

**已知缺口**：`retry_count`/`replan_count` 只在 `AgentState` 字段，未落库、未上指标。执行成功率按"预期终态"判定（timeout 场景本来就该 FAILED，不计为失败）；真正的重试率/恢复率需新增生产指标（见 §5）。

### D3 可观测性指标验证（observability）

| 指标 | 定义 | 测量方法 | 验收标准 | 当前基线 |
|---|---|---|---|---|
| 指标族暴露 | 5 族计数器/直方图/仪表齐全 | 进程内 `create_metrics()` 注册表 | 5 族齐全 | 单测覆盖 |
| phase_latency 标签 | histogram 的 phase 标签 | 检查 labelnames | plan/execute/verify | 单测覆盖 |
| `/metrics` 路由 | API 暴露 Prometheus 文本 | 扫描 `apps/api/routes/metrics.py` | 已注册 | 有 |
| Trace 端到端接线 | 生产代码 `node_span(` 调用点 | 静态扫描 src/ + apps/ | **≥1 处** | **7 处**（任务 7.1：worker 图 7 节点统一接线） |
| LLM 采集接线 | 生产代码 `llm_call(` 调用点 | 静态扫描 src/ + apps/ | **≥1 处** | **已接线**（任务 7.2：LLM 调用点接 `llm_call`，token 回填经生产走线） |
| 结构化日志 | TraceContext 贯穿 JSON 日志 | 复用现有单测 | 覆盖 | 覆盖 |

**教学点**：可观测性指标不是"指标本身的值"，而是"采集链路是否真的通了"。`node_span`/`llm_call` 已有实现与单测，但生产代码无调用点——单测只能证明"装饰器会记 span"，证明不了"真实 Run 产生了 span"。这是"测试绿 ≠ 端到端通"的典型例子，必须诚实标为缺口。

### D4 工程过程指标（engineering process）

| 指标 | 定义 | 测量方法 | 验收标准 | 当前基线 |
|---|---|---|---|---|
| 代码规模 | src/apps/tests 的 .py 数与总行数 | ast/Path 统计（零依赖） | 记录 | src 71 / apps 36 / tests 126 |
| 单测函数数 | `def test_*` 数量 | ast 扫描 tests/ | 记录 | 1445 |
| 行覆盖率 | 被测代码行占比 | pytest-cov（需依赖） | ≥ 80% | **96.14% PASS** |
| 类型覆盖 | mypy 干净文件数 | `mypy src/ apps/` | 103 文件 | 103 |
| 大函数数 | 超过 50 行的函数 | ast 扫描 | 记录 | 无 |

**教学点**：为什么覆盖率用 ast 手工统计代码规模、用 pytest-cov 测覆盖率？代码规模是纯静态事实，用标准库 `ast`/`Path` 即可，零依赖、快；覆盖率需要跟踪**执行轨迹**，必须靠插桩工具（pytest-cov 封装 `coverage.py`）。`coverage.py` 原理是字节码级插桩——每个 `line` 事件计数，结束时按文件汇总"执行过的行/总行"。这解释了为什么"未测到"与"不存在"不同：覆盖率 80% 意味着有 20% 的行从未执行，它们是潜在的隐藏 bug 区。

## 4. 统一评测入口

`evals/run_engineering_metrics.py` 聚合四个 collector，输出统一 JSON 报告 + 文本表格：

```bash
uv run python evals/run_engineering_metrics.py \
    --report evals/reports/engineering_metrics_<时间戳>.json
```

**输出结构**：

```json
{
  "generated_at": "...",
  "groups": { "quality": {"group":"quality","status":"...","metrics":[...]}, ... },
  "gaps": [ {"id":"...","title":"...","detail":"..."} ],
  "overall_status": "PASS | PARTIAL | SKIPPED | FAIL"
}
```

**status 语义**：`PASS`（达标）/ `FAIL`（未达标）/ `MEASURED`（仅记录，无合格线）/ `SKIPPED`（缺测试库等环境无法运行）/ `NOT_CONFIGURED`（缺工具）。

**设计原则**：

- **复用而非重写**：系统质量直接调 `run_all(CATEGORY_RUNNERS)`（DB 依赖的 knowledge_rag 失败进 `errors`，不中断其余）；故障注入调 `fault_injection.run_all()`；端到端复用 `tests/e2e` 的 TestClient+eager 模式。
- **DB 依赖组 SKIPPED 而非失败**：e2e 与 knowledge_rag 需 Postgres 测试库，连不上则整组如实 `SKIPPED`，绝不中断确定性 collector 的结果。
- **诚实缺口**：Trace/LLM 接线为 0 时报告 `FAIL` 并进 gaps 段，不是假装通过。

## 5. 缺口注册（已知限制，计划后续任务）

> 本表为四维工程化指标（D1-D4）的已知缺口。**承接计划**：`docs/10-detailed-task-list.md` 阶段七（7.1-7.11）已逐条落任务，本表「承接任务」列据此更新；未承接的缺口保持「计划后续任务」。

> **状态（2026-08-20）**：阶段七（7.1-7.11）已全部完成，下表缺口**全部闭合**；D1-D4 四组 `engineering_metrics.json` overall **PASS**、gaps 0。

| # | 缺口 | 承接任务 | 状态 |
|---|---|---|---|
| 1 | Trace/LLM 采集未端到端接线 | **任务 7.1**（worker 图 7 节点挂 `@node_span`）+ **任务 7.2**（LLM 调用点接 `llm_call`，回填 token） | ✅ 已闭合 |
| 2 | 无重试率/恢复率指标 | **任务 7.3**（`erp_run_retries_total` / `erp_run_replans_total` / `erp_run_abandoned_total`，recover_or_replan 三分支自增） | ✅ 已闭合 |
| 3 | 行覆盖率 | pytest-cov，实测 96.14% PASS | ✅ 已闭合 |
| 4 | mypy 既有错误 | `mypy src/ apps/` 103 文件 0 错误 | ✅ 已闭合 |
| 5 | 阶段延迟 plan/verify 未实测 | **任务 7.5**（`perf_counter` 修复 Win11 时钟粒度 0ms 假象） | ✅ 已闭合 |
| 6 | 无审批/人工介入率指标 | **任务 7.4**（`erp_approval_requests_total` + outcome 标签） | ✅ 已闭合 |
| 7 | 无按 Tier 分层成功率 | **任务 7.6**（runs 计数器加 tier 标签 + `error_taxonomy.py` 聚合） | ✅ 已闭合 |
| 8 | 无 token/成本/单 Run LLM 调用次数 | **任务 7.7**（`llm_usage_report.py` 按价表重估） | ✅ 已闭合 |
| 9 | 无计划采纳率/修正率 | **任务 7.8**（`erp_plan_outcome_total{accepted/edited/rejected}`） | ✅ 已闭合 |
| 10 | 无在线 groundedness/拒答率 | **任务 7.9**（`erp_answer_grounded_total{yes/no/refused}`，`classify_answer_groundedness`） | ✅ 已闭合 |
| 11 | LLM 评测集规模不足（50 条） | **任务 7.10**（扩至 200+、分层 CI、漂移检测） | ✅ 已闭合 |
| 12 | 写路径端到端未启用 | **任务 7.11**（`erp_reconciliation_success_total{consistent/mismatch}`，审批→幂等→对账） | ✅ 已闭合 |

## 6. 复现与报告

```bash
# 统一评测（四个维度一次跑完）
uv run python evals/run_engineering_metrics.py

# 分项复现（与 benchmark.md 一致）
uv run evals/run_all.py
uv run python tests/performance/fault_injection.py
uv run python evals/scripts/run_security_eval.py
uv run python evals/scripts/run_ablation.py
uv run pytest tests/ -q
uv run mypy src/ apps/
uv run ruff check . && uv run ruff format --check
```

报告固化在 `evals/reports/`（gitignored），逐位可复现。

## 7. 迭代节奏

每个评测结果对应一个验收标准。当指标从 PASS 掉到 FAIL 时，先定位是**产品回归**还是**评测失真**（如知识库改了导致 RAG 召回下降），再决定修产品还是修评测。评测计划本身也按 `docs/09-implementation-roadmap.md` 的迭代节奏演进：先定指标，再补工具，最后固化成发布 Gate（对应 docs/08 §10）。
