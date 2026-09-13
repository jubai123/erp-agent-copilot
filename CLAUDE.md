> **MCP code-review-graph 使用说明已通过 SessionStart hook 自动注入上下文，此处不再赘述。**

# ERP Agent Copilot V6 — 规则总入口

本文件是两类工作场景**共用**的规则文档，同时是场景规则文档的索引。

设计意图：规则按场景拆分，每次会话**只加载当前场景需要的那一份**，
这样 Claude Code 能把注意力聚焦在当前任务上，也避免无关规则占用上下文。
所以本文件不写具体场景的做法，只写「怎么找到并加载对应规则」+ 两类场景都要遵守的共用内容。

## 一、场景路由（动手前必须先做这一步）

### 1.1 判断场景

| 场景 | 触发特征 | 必须读取的规则文档 |
| ---- | -------- | ------------------ |
| **开发场景** | 要改东西：加功能、修 bug、写测试、重构、跑迁移、提交 Git | `.claude/rules/dev.md` |
| **问询场景** | 只问不改：问原理、问架构、问实现细节、问进度、要讲解、要 review 但不要求改动 | `.claude/rules/query.md` |

### 1.2 执行规则

1. **先读后做**：判断出场景后，第一件事是用 Read 打开对应的规则文档。**读之前不要动手。**
2. **只加载一份**：只读当前场景那一份，另一份**不要读**（这正是拆分的目的）。
3. **场景叠加**：一次任务里既要讲解又要改代码 → 判定为**开发场景**，读 `dev.md`。
4. **场景不明**：先问用户，不要猜。典型模糊请求「能不能顺便改一下」→ 按开发场景处理。
5. **优先级**：本文件（共用层）> 场景规则文档。冲突时以本文件为准。

> **为什么必须手动 Read？**
> `.claude/rules/*.md` 是 Claude Code 的原生约定目录，**不带 `paths` frontmatter 的规则会在每次会话启动时自动加载**。
> 那样两份规则会同时常驻上下文，导致规则串味（例：在纯问询任务里按 `dev.md` 去写测试、改代码）。
> 因此本项目在 `.claude/settings.json` 里用 `claudeMdExcludes` 把它排除出自动加载，
> 改为由本文件按场景按需读取 —— **每次会话只让一份规则进入上下文**。
> 排除值必须是 `**/agent-copilot-v6/.claude/rules/**` 这种**带 `**/` 前缀**的形式：
> 该配置匹配的是**绝对路径**，写成相对路径 `.claude/rules/**` 会静默失效（不报错，但排除不了）。
> 如果你发现 `.claude/rules/` 下的内容已经在上下文里了（可用 `/context` 的 Memory files 节核对），说明排除没生效。

### 1.3 索引

| 文档 | 路径 | 内容 | 状态 |
| ---- | ---- | ---- | ---- |
| 开发场景规则 | `.claude/rules/dev.md` | 角色定位、核心开发原则、开发流程、禁止事项、教学检查点 | 已就绪 |
| 问询场景规则 | `.claude/rules/query.md` | 问询场景的角色、回答标准、工具范围、场景切换边界 | **待完善** |

## 二、项目技术栈

| 层       | 选择                                  | 原因（教学要点）                      |
| -------- | ------------------------------------- | ------------------------------------- |
| API      | FastAPI + Pydantic v2                 | 异步、类型契约、自动生成 OpenAPI 文档 |
| Agent    | LangGraph + Typed State               | 状态图建模，比顺序 while 循环更安全   |
| 异步任务 | Celery + Redis                        | 适合长任务、重试、队列和取消          |
| 关系数据 | PostgreSQL + SQLAlchemy + Alembic     | 事务保证、版本管理和迁移              |
| 向量检索 | pgvector + PostgreSQL FTS             | 本地部署简单，支持混合检索            |
| 工具协议 | MCP Python SDK                        | AI Agent 标准工具协议                 |
| 重排序   | Cross-Encoder / Qwen Rerank           | 延续 V5 的两阶段检索优势              |
| 可观测性 | OpenTelemetry + Langfuse + Prometheus | 分布式调用、LLM Trace 和系统指标      |
| 测试     | pytest + testcontainers + respx       | 单元、集成和 HTTP Mock                |
| 工程质量 | uv, ruff, mypy, pre-commit            | 可复现环境和代码规范                  |

## 三、目录结构约定

```
erp-agent-copilot-v6/
├── apps/                    # 进程入口（api, worker, mcp_gateway, erp_simulator）
├── src/erp_copilot/
│   ├── domain/              # 领域实体、枚举、错误模型（不依赖任何框架）
│   ├── application/         # 用例和事务边界
│   ├── agent/               # LangGraph 状态图、Planner、Executor、Verifier
│   ├── retrieval/           # 文档导入、混合检索、Rerank、引用
│   ├── tools/               # Tool Registry、OpenAPI 导入、MCP、Executor
│   ├── memory/              # Checkpoint、摘要、上下文预算
│   ├── security/            # RBAC、Policy、SSRF、Redaction
│   └── infrastructure/      # 数据库、Redis、LLM、VectorStore 适配器
├── evals/                   # 评测数据集、评分器和报告
├── tests/                   # unit, integration, e2e, eval, security, performance
├── docs/                    # 产品、架构、API 契约、威胁模型、评测、Benchmark、ADR
├── infra/                   # docker-compose.yml, prometheus, grafana
├── .claude/rules/           # 场景规则文档（dev.md, query.md）
├── pyproject.toml
├── uv.lock
└── README.md
```

## 四、会话历史记录规则

**触发条件**：收到用户结束对话的指令（如”结束对话””退出””关闭会话”等）。**两类场景都适用。**

**执行动作**：

1. 在 `session-history/` 目录下创建文件，命名格式：`{两位序号}-{简短描述}.md`（如 `01-development-plan.md`）
2. 序号从已有文件中自动递增（第一个会话为 01）
3. 文件内容包含：
   - 会话日期
   - 会话目标（1-2 句话）
   - 完成的任务列表（含文件路径）
   - 关键决策和原因
   - 下一步要做的事
   - 重要提示（如有需要提醒新会话的注意事项）

**目的**：让新会话能快速理解之前的进度，无需用户重复描述。
