# 评测、可观测性与性能

## 1. 评测原则

- 固定数据集、代码版本、模型、Prompt和检索配置。
- CI默认使用Mock LLM与Record/Replay Tool，避免付费服务波动。
- 指标按任务类型分组，不能只报告总体平均值。
- 保留失败Case和原始Trace，不只展示最高分。
- 数据生成、调参和最终测试严格隔离。

## 2. 200条评测集构成

| 类型 | 数量 | 核心检查 |
|---|---:|---|
| Tool Retrieval | 40 | Recall@5、MRR、混淆Tool和Hard Negative |
| Knowledge RAG | 40 | Recall@5、NDCG、引用和无答案拒答 |
| Single/Multi-step Planning | 50 | Plan合法性、依赖、参数和任务成功率 |
| Missing Param/Recovery | 25 | 产品、数量、地区等缺参、冲突确认、重试和Checkpoint |
| Security | 25 | 注入、越权、SSRF、Secret和危险订单操作 |
| Failure/Cancellation | 20 | Worker恢复、ERP Tool故障、取消、deadline和对账 |
| 合计 | 200 | 端到端能力闭环 |

## 3. AI与Agent指标

- Tool Recall@1、Recall@5、MRR、NDCG。
- RAG Context Recall、引用正确率和无答案拒答率。
- Plan Valid Rate和参数来源完整率。
- Step Success Rate和Task Success Rate。
- Replan Rate、平均Tool次数和平均Token。
- Security Attack Block Rate和False Positive Rate。
- Worker Recovery Rate和重复订单写操作次数。

## 4. 系统指标

- API enqueue P50和P95。
- Tool Retrieval P50和P95。
- LLM、MCP和Tool调用P50、P95及错误率。
- 端到端任务P50、P95，按单步和多步拆分。
- 指定并发下吞吐量、任务丢失率和队列长度。
- PostgreSQL连接池、Redis延迟和Worker利用率。
- 单任务Token、模型成本和Artifact大小。

## 5. Trace设计

一个`run_id`必须串联：

```text
HTTP request
  → enqueue
  → worker task
  → LangGraph node
  → retrieval
  → rerank
  → LLM call
  → policy check
  → MCP call
  → external tool
  → verification
```

Span属性至少包含租户匿名标识、Run、Step、Tool版本、模型、Token、延迟、重试次数和结果状态。不得记录完整密钥或未经脱敏的Tool响应。

## 6. 结构化日志

日志字段：

- `timestamp`、`level`、`service`。
- `trace_id`、`span_id`、`run_id`、`step_id`。
- `tenant_id_hash`、`tool_version_id`。
- `event_type`、`error_code`、`duration_ms`。

禁止使用无法关联Run的自由文本日志作为唯一诊断依据。

## 7. 失败分类

- Tool召回错误。
- 知识召回或引用错误。
- Planner结构或依赖错误。
- 参数提取、来源或冲突错误。
- Policy和审批错误。
- Tool超时、限额或业务错误。
- Verifier误判。
- Checkpoint、恢复或幂等错误。
- 安全漏报或误报。
- 上下文超限和模型输出错误。

每次Eval报告输出失败数量、占比、代表Case和修复优先级。

## 8. 消融实验

至少执行：

- Vector-only、Hybrid、Hybrid+Rerank。
- 无Verifier与有Verifier。
- 顺序执行与并行只读Step。
- 无上下文压缩与有上下文预算。
- 无Record/Replay与固定Replay的稳定性对比。

一次实验只改变有限变量，并保存配置快照。

## 9. 性能与故障测试

使用固定Mock LLM执行Locust测试，至少覆盖：

- 1、10、25和50个并发客户端。
- 单Tool任务和多Tool任务。
- Tool延迟、429、5xx和超时。
- Worker强制终止和恢复。
- PostgreSQL或Redis短暂不可用。
- 大Tool结果转Artifact。

## 10. 发布Gate

### 检索Gate

- 至少60条人工标注检索Case。
- 三组检索消融报告可复现。
- 引用可以追溯到文档和版本。

### Runtime Gate

- 诊断、审批、取消、重试和恢复可演示。
- 未审批订单写操作调用次数为0。
- Worker恢复后重复订单创建或更新次数为0。

### 安全与评测Gate

- 200条Eval可一键执行。
- 正常、无工具、错误参数、超时、注入、越权和恢复均有覆盖。
- 一次失败可从Trace定位到具体节点。

### [REDACTED]Gate

- 一键部署、一键评测和一键负载测试。
- 所有[REDACTED]数字都有报告、配置和原始结果。
- 仓库不包含密钥、真实敏感数据和虚假Benchmark。
