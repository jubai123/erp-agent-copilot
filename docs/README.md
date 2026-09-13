# ERP Agent Copilot V6 技术文档

本目录是 Agent Copilot V6（ERP业务流程智能编排平台）的实现依据，覆盖产品范围、架构、Agent Runtime、知识检索、工具执行、安全、数据模型、评测和数据集构建。

V6 不在 V5 的 Flask、MongoDB 和进程内任务模型上继续堆功能，而是保留ERP工具检索、Rerank、参数提取、多步API编排和MCP等有效思路，重新建立可审批、可恢复、可评测的企业ERP Agent控制面。

## 1. 项目目标

将自然语言ERP业务意图转换为可解释、可校验的 Tool DAG，跨库存、物流供应商和订单系统完成业务流程；创建订单、更新状态等高风险操作必须经过权限与人工审批，并能在工具超时或Worker重启后恢复执行。

首版重点证明三个场景：

1. 多系统下单：检查苹果库存，查询发往上海的供应商，选择合适方案并创建20KG订单。
2. 业务写操作审批：对创建订单、更新订单状态等写操作进行参数校验、权限检查和人工确认。
3. 异常与安全恢复：处理缺参、库存不足、供应商查询失败、API超时、提示注入和Worker重启，不产生重复订单。

## 2. 文档导航

### 技术设计

- [01-product-and-scenarios.md](01-product-and-scenarios.md)：产品范围、演示故事和验收标准。
- [02-system-architecture.md](02-system-architecture.md)：系统边界、组件、部署和依赖方向。
- [03-agent-runtime.md](03-agent-runtime.md)：AgentState、Plan DAG、状态机、Checkpoint和恢复。
- [04-knowledge-and-retrieval.md](04-knowledge-and-retrieval.md)：知识分层（L1 Rules + L2 RAG）、混合检索、Rerank和引用。
- [05-tools-mcp-and-simulator.md](05-tools-mcp-and-simulator.md)：Tool Registry、工具候选过滤、MCP Gateway、ERP Simulator和接口限额处理。
- [06-security-approval-and-recovery.md](06-security-approval-and-recovery.md)：RBAC、审批、SSRF、幂等、安全和故障恢复。
- [07-data-model-and-api-contracts.md](07-data-model-and-api-contracts.md)：核心数据表、API、事件和错误契约。
- [08-evaluation-observability.md](08-evaluation-observability.md)：离线评测、Trace、指标、性能和发布Gate。
- [09-implementation-roadmap.md](09-implementation-roadmap.md)：实施顺序、迁移策略和阶段交付物。
- [10-detailed-task-list.md](10-detailed-task-list.md)：精细化开发计划与任务列表（含教学要点）。
- [11-architecture-decisions.md](11-architecture-decisions.md)：架构决策记录（ADR），每个决策含原因、取舍、评测和保留选项。
- [13-final-delivery-summary.md](13-final-delivery-summary.md)：最终交付总结——87 项任务全部完成、实测数字与诚实边界。

### 数据集构建

- [data/01-knowledge-base-dataset.md](data/01-knowledge-base-dataset.md)：ERP业务知识库的数据来源与构建方法（L1 Rules + L2 文档）。
- [data/02-simulator-dataset.md](data/02-simulator-dataset.md)：商品、库存、供应商、订单和异常场景数据构建。
- [data/03-evaluation-dataset.md](data/03-evaluation-dataset.md)：200条评测数据的Schema、标注、切分和质量控制。

## 3. 推荐仓库结构

```text
erp-agent-copilot-v6/
├── apps/
│   ├── api/
│   ├── worker/
│   ├── mcp_gateway/
│   └── erp_simulator/
├── src/erp_copilot/
│   ├── domain/
│   ├── application/
│   ├── agent/
│   ├── retrieval/
│   ├── tools/
│   ├── memory/
│   ├── security/
│   ├── observability/
│   └── infrastructure/
├── datasets/
│   ├── knowledge/
│   │   ├── rules/           # L1：结构化 Rules + 意图→Rule 映射表
│   │   └── domain-model/...  # L2：知识文档
│   ├── simulator/
│   └── evaluation/
├── tests/
├── infra/
└── docs/
```

## 4. 技术基线

| 领域 | 基线技术 |
|---|---|
| API | FastAPI、Pydantic v2 |
| Agent | LangGraph、强类型AgentState |
| 异步任务 | Celery、Redis |
| 事务数据 | PostgreSQL、SQLAlchemy、Alembic |
| 检索 | PostgreSQL全文检索、pgvector、Cross-Encoder或Qwen Rerank |
| Tool协议 | Official MCP Python SDK、OpenAPI 3.x |
| 可观测性 | OpenTelemetry、Langfuse、Prometheus |
| 测试 | pytest、testcontainers、respx、Locust |
| 工程质量 | uv、ruff、mypy、pre-commit |

## 5. 数据边界

- 知识库保存ERP业务规则、API说明、字段语义、订单流程、供应商选择规则和安全策略。
- ERP Simulator或真实Tool提供当前商品库存、供应商、订单和业务系统状态。
- PostgreSQL保存Run、Step、审批、事件、审计和评测结果。
- 大型Tool结果保存为Artifact，数据库只保存索引、摘要和校验信息。
- 任何真实密钥、个人敏感信息和生产数据不得进入仓库或评测集。

## 6. 文档维护规则

1. 产品行为变更先更新产品与API契约，再修改代码。
2. 状态机、权限、幂等和数据表变更必须补充对应测试。
3. 新依赖或重要架构决策使用ADR记录原因和替代方案。
4. 所有性能、检索和安全数字必须来自可复现报告。
5. 文档中的“目标值”不等于项目已经达到的实测值。
