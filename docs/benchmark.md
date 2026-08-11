# 基准测试与性能报告：ERP Agent Copilot V6

> 最终交付文档。**本页只写真实测量值，所有数字都能复现**（`docs/README.md` 规则 5：目标值不等于实测值）。未测量的项明确标注"待实测"，不编造数据。原始报告见 `evals/reports/`。

## 1. 汇总

| 项 | 结果 | 证据 |
|---|---|---|
| 单元测试 | 874 通过 | `uv run pytest tests/unit` |
| 类型检查 | 55 个 src 文件 mypy 干净 | `uv run mypy src` |
| 静态检查 | ruff check / format 通过 | `uv run ruff check .` / `uv run ruff format --check` |
| 故障注入 | 3/3 场景 PASS | `uv run python tests/performance/fault_injection.py` |
| 检索消融 | 42 条查询 × 3 配置 | `evals/reports/ablation_after_keyword_fix.json` |
| 负载测试 | 50 并发/60s：1385 请求 0 失败，吞吐 23.25 req/s，P50 20ms / P95 44ms / P99 57ms | 本机实测，详见 §5 |

## 2. 检索消融（42 条查询，本机实测）

同一 42 条查询在三种检索配置下的离线指标（向量 / 混合 / 混合+Rerank）：

| 配置 | Recall@1 | Recall@5 | MRR | NDCG@5 | P@5 | P50(ms) | P95(ms) |
|---|---|---|---|---|---|---|---|
| Vector-only | 0.3968 | 0.8571 | 0.8381 | 0.7791 | 0.4103 | 49.78 | 53.97 |
| Hybrid | 0.3532 | 0.7381 | 0.7937 | 0.6824 | 0.3111 | 52.79 | 55.70 |
| Hybrid+Rerank | 0.3770 | **0.9167** | 0.8294 | **0.8249** | **0.4187** | 226.04 | 245.28 |

**解读（诚实）**：

- **Rerank 的增量在 Recall@5**：0.7381 → 0.9167（+0.18），因为 Cross-Encoder 把关键词误召回的高分噪声压下、把相关文档顶到前 5。
- **Rerank 的代价是延迟**：P50 从 ~53ms 涨到 ~226ms（约 4 倍）。这是本地 CPU 推理 Cross-Encoder 的实测值。
- **Vector-only 的 Recall@1 反而最高（0.3968）**：说明语义向量对"第一个最相关文档"最敏锐；混合把 FTS 命中混进来后首位置被噪声稀释，Rerank 又部分找回。消融的价值正在于此——不跑数据，"Rerank 一定更好"是错觉。
- **L1 Skill 覆盖率：1.0**（42/42 查询均命中 `intent_skill_map.yaml` 的意图→技能映射）。

复现：

```bash
uv run python evals/scripts/run_ablation.py
```

## 3. 安全评测（25 条用例，真实守卫）

| 指标 | 值 |
|---|---|
| 攻击拦截率 | 待列出：见 `uv run python evals/scripts/run_security_eval.py` 输出 |
| 误报率 | 同上 |

> **诚实标注**：25 条安全用例的逐条结果每次运行可复现（SSRF 用伪造 DNS，无网络依赖），但本报告未在本机固化一次运行的 JSON 输出；`run_all` 聚合报告同样如此。为不编造数字，这里不写具体百分比，运行命令即得。

## 4. 故障注入与恢复（3/3 PASS）

`tests/performance/fault_injection.py` 建模 docs/08 §9 的三个故障场景，组件边界确定性复现（无 Postgres/Redis/LLM）：

| 场景 | 结论 | 断言 |
|---|---|---|
| worker_recovery | PASS | 崩溃后从 checkpoint 恢复，仅对 PENDING 的 s1 对账并标 `RECOVERY_RECONCILIATION_REQUIRED`，COMPLETED 的 s2 永不重跑 |
| erp_timeout_retry | PASS | 504 重试至 201，同幂等键返回同一订单，库存只扣一次 |
| order_idempotency | PASS | 同键两次 POST → 201/200 同一 order_id，库存恰好扣一次 |

负例证明 harness 不虚绿：无崩溃注入时 `passed=False`、无超时注入时 `passed=False`（见 [test_fault_injection.py](../tests/unit/performance/test_fault_injection.py)）。

复现：

```bash
uv run python tests/performance/fault_injection.py
```

## 5. 负载测试（本机实测，2026-08-11）

场景逻辑就绪（[load_scenario.py](../tests/performance/load_scenario.py) + [locustfile.py](../tests/performance/locustfile.py)），提交路径不发 LLM 调用（docs/08 §9 的"固定 Mock LLM"要求天然满足——只有 ERP Simulator 在跑）。

前置条件：Postgres + Redis 启动、API 在 :8000、种子租户（`loadtest-tenant`，幂等 INSERT）。

```bash
uv run locust -f tests/performance/locustfile.py \
    --headless -u 50 -r 5 -t 60s \
    --html tests/performance/reports/locust_report.html \
    --host http://127.0.0.1:8000
```

> **Host 用 `127.0.0.1` 而非 `localhost`**：Windows 上 `localhost` 同时解析到 `::1` 与 `127.0.0.1`，客户端先试 IPv6 再回退 IPv4，每次请求多 ~2s 假延迟，P95 呈双峰；`127.0.0.1` 直接走 IPv4，得到干净数字。

> **状态**：**实测完成**。50 用户 / 60s，`POST /v1/runs`（DB INSERT×2 + Celery Redis enqueue，返回 202）：
>
> | 指标 | 数值 |
> | --- | --- |
> | 总请求 / 失败 | 1385 / 0 |
> | 吞吐 | 23.25 req/s |
> | 平均 / P50 | 21.7 ms / 20 ms |
> | P95 / P99 | 44 ms / 57 ms |
> | 最大 | 69 ms |
>
> 环境：Windows 11、Postgres 与 Redis 同机、单进程 uvicorn、目标库为测试库 `erp_copilot_test`（绝不指回开发库）。原始 HTML 报告由上述命令即时生成，不入库。

## 6. 复现全部数字

```bash
uv run pytest tests/unit                       # 874 通过
uv run mypy src                                 # 55 文件干净
uv run ruff check . && uv run ruff format --check
uv run python tests/performance/fault_injection.py   # 3/3 PASS
uv run python evals/scripts/run_ablation.py          # 42 查询消融
uv run evals/run_all.py                              # 200 条六大分类
uv run python evals/scripts/run_security_eval.py     # 25 条安全守卫
```
