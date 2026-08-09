# 实施路线与迁移策略

> 架构决策摘要见 [11-architecture-decisions.md](11-architecture-decisions.md)。阶段三/四任务细节见 [10-detailed-task-list.md](10-detailed-task-list.md)。

## 1. 总体策略

V5冻结为参考和基线，不直接在原有Flask项目中大规模重构。V6使用新目录或新仓库实现，通过Adapter复用经过验证的数据和算法思想。

推荐顺序：

```text
产品与评测定义
  → 工程骨架和数据模型
  → Tool与模拟器
  → 知识检索
  → Agent Runtime
  → 审批、安全和恢复
  → 可观测性与评测
  → 性能与交付
```

## 2. 阶段一：需求和工程骨架

交付物：

- 三个Demo场景及可测试验收标准。
- Run状态、PlanStep和统一错误模型。
- FastAPI、PostgreSQL、Redis和Alembic骨架。
- uv、ruff、mypy、pytest、pre-commit和CI。

验收：Docker Compose启动成功，迁移可重复执行，单元测试不依赖外部网络。

## 3. 阶段二：Tool与模拟器

交付物：

- Tool、ToolVersion、Scope和风险模型。
- OpenAPI 3.x导入和Schema校验。
- MCP Gateway持久连接和统一ToolResult。
- ERP Simulator的商品、库存、供应商和订单Tool。
- ERP Mock、Replay和显式Live模式。

验收：可导入V5 OpenAPI数据；模拟器支持库存充足、库存不足、供应商不可用和订单写入超时场景；写Tool具备幂等测试。

## 4. 阶段三：知识检索

交付物：

- 统一事实表（先于文档，对齐模拟器世界状态）。
- Markdown和文本导入任务。
- 文档版本、元数据、Chunk和去重。
- L1 Skill 定义（10-15 个：状态机、参数约束、审批策略）和意图→Skill 映射表。
- L2 RAG 数据集（15-25 份文档，8 类目录）。
- PostgreSQL全文检索、pgvector和Rerank。
- 来源引用和上下文装配。
- 40条Tool/RAG检索评测Case + L1遵循评测。

验收：能够输出Top-K、各阶段分数和引用；完成Vector、Hybrid和Rerank消融及L1开关对比。

## 5. 阶段四：Agent Runtime

交付物：

- Typed AgentState和LangGraph节点。
- 结构化Planner和DAG校验器。
- **工具候选过滤**：意图→域确定性过滤（主引擎）+ 向量精排接口（按需，预留在工具数增长时启用）。
- **L1/L2 注入**：build_plan Prompt 按 System → L1 Skill → L2 检索 → 候选工具 → Query 顺序注入。
- 并行READ、串行WRITE的Executor。
- 参数来源、缺参确认和上下文预算。
- Verifier、replan上限和Checkpoint。

验收：只读多步骤任务可完成；非法DAG被拒绝；Run可以暂停并恢复。

## 6. 阶段五：审批、安全与恢复

交付物：

- RBAC、租户隔离和Tool Scope。
- Policy Engine和审批API。
- SSRF、egress allowlist、Redaction和响应限制。
- 幂等、重试、deadline、取消和Worker恢复。
- 安全与恢复测试集。

验收：未审批订单写操作不执行；越权返回403；Worker重启不重复创建或更新订单。

## 7. 阶段六：评测和交付

交付物：

- 200条Eval数据集。
- Record/Replay和失败分类。
- OpenTelemetry、Langfuse和Prometheus。
- Locust负载、故障恢复和成本报告。
- Swagger或最小Web Console、README和演示视频。

验收：10分钟内连续演示库存校验、供应商选择、订单创建、审批、攻击拦截和恢复；所有指标可复现。

## 8. V5迁移清单

| V5资产 | 迁移方式 |
|---|---|
| `dataset_apis_aliyun.json` | 作为OpenAPI导入、工具检索和兼容性测试基线 |
| 工具意图训练数据 | 转换为V6 Tool Retrieval数据格式并重新切分 |
| 向量召回与Rerank经验 | 作为Vector-only和Rerank基线，不直接复制数据库封装 |
| 参数提取Prompt | 转为结构化输出基线，补参数来源和Schema校验 |
| MCP客户端思想 | 改为持久Gateway和连接复用 |
| Flask路由、线程池 | 不迁移 |
| MongoEngine实体 | 不迁移，按V6事务模型重新建模 |
| 进程内JWT缓存 | 不迁移，使用可审计身份与权限模型 |

## 9. 实施纪律

1. 每个Issue只有一个可验证目标。
2. 先添加失败测试，再实现最小功能。
3. 单次修改尽量控制在3至6个文件。
4. 新依赖必须说明用途和替代方案。
5. 安全、并发、幂等和Trace是功能验收的一部分。
6. 前端美化不得早于核心评测和恢复能力。
7. 微调只在检索基线证明必要时进行。

## 10. 首个可执行迭代

建议第一个迭代只完成：

- `POST /v1/runs`创建只读Run。
- 一个固定Plan和两个并行读取Tool。
- 一个`order_happy_path`模拟场景。
- Run事件、结果和Trace。
- 10条端到端评测Case。

这个垂直切片验证API、队列、状态、Tool、模拟数据和评测的完整链路，然后再引入LLM Planning、RAG和审批。
