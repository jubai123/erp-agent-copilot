# ERP Agent Copilot V6：精细化开发计划与任务列表

> 基准参考：V5 实现（`agent-copilot-v5-20260317/`）和 V6 设计文档（`docs/01-09`）
>
> 教学理念：每完成一个小任务，理解一个技术点。宁可慢，不可跳。

---

## 总体原则

1. **每次只做一个微小的增量**：一个任务 = 一个小功能 = 一个 commit。
2. **教学先行**：每进入一个新阶段，先用一段话讲清楚「这个阶段要解决什么问题、用到什么技术、技术原理是什么」。
3. **TDD 铁律**：先写一个会失败的测试，再写让它通过的最小代码。
4. **修改范围控制**：单个任务不超过 6 个文件。

---

# 阶段一：工程骨架（预计 10-12 个任务）

> **阶段教学引言**
>
> 任何软件项目的第一步都不是写业务代码，而是搭建「工程骨架」——就像盖房子要先打地基、搭脚手架。
> 工程骨架包括：项目结构、依赖管理、代码质量工具、数据库模型和 Docker 开发环境。
>
> 这个阶段你会学到：
> - **uv**：新一代 Python 包管理器，比 pip 快 10-100 倍，有锁文件保证可复现
> - **FastAPI vs Flask**：为什么 V6 弃用 Flask？因为 FastAPI 原生支持异步（async/await），
>   能自动生成 OpenAPI 文档，类型校验更严格
> - **PostgreSQL vs MongoDB**：V5 用 MongoDB 存任务状态，V6 改用 PostgreSQL。
>   因为 Agent 的 Run、审批、事件需要事务保证（ACID），MongoDB 的灵活性在这里是劣势
> - **Alembic**：数据库迁移工具，像 Git 一样管理数据库表结构的版本
> - **Docker Compose**：一键启动 API + PostgreSQL + Redis 等所有依赖

---

## 任务 1.1：初始化项目仓库和 uv 环境

**目标**：创建 V6 项目骨架，配置 uv 包管理器。

**交付物**：
- `pyproject.toml`（项目元数据、Python 版本、依赖声明）
- `uv.lock`（锁定依赖版本，保证任何人安装都一致）
- `.python-version`（指定 Python 3.12）

**涉及文件**（3 个）：
- `pyproject.toml`
- `uv.lock`
- `.python-version`

**验收标准**：
- `uv sync` 成功安装所有依赖
- `uv run python -c "import fastapi; print(fastapi.__version__)"` 正常输出

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| uv 是什么 | Rust 写的 Python 包管理器，替代 pip + virtualenv + pip-tools |
| 为什么不用 pip | pip 没有锁文件，不同机器可能安装不同版本导致"在我机器上能跑" |
| pyproject.toml 是什么 | Python 项目的"身份证"，统一管理依赖、配置、构建 |

---

## 任务 1.2：配置 ruff、mypy、pre-commit

**目标**：建立代码质量防线。

**交付物**：
- `pyproject.toml` 中 `[tool.ruff]`、`[tool.mypy]` 配置段
- `.pre-commit-config.yaml`

**验收标准**：
- `uv run ruff check .` 零错误
- `uv run mypy src/` 类型检查通过
- `pre-commit run --all-files` 全部通过

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| ruff 是什么 | Python 的 linter + formatter，Rust 写的，速度是 flake8 的 10-100 倍 |
| mypy 是什么 | 静态类型检查器，在不运行代码的情况下发现类型错误 |
| pre-commit 是什么 | Git hook 框架，每次 commit 前自动运行检查，阻止坏代码入库 |

---

## 任务 1.3：建立领域层——错误模型和枚举

**目标**：定义项目中所有模块共享的**统一错误模型**。

**交付物**：
- `src/erp_copilot/domain/__init__.py`
- `src/erp_copilot/domain/errors.py`（`CopilotError` 基类、`ValidationError`、`AuthorizationError` 等）
- `src/erp_copilot/domain/enums.py`（`RunStatus`、`ToolRiskLevel`、`StepStatus` 等枚举）
- `tests/unit/domain/test_errors.py`

**验收标准**：
- 每种错误类型可通过 `isinstance` 区分
- 错误信息包含 `message`、`code`、`details`
- 枚举值之间比较正常工作

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 领域层是什么 | 洋葱架构的最内层，定义业务规则，**不依赖任何框架或数据库** |
| 为什么需要统一错误模型 | V5 在不同模块中各自抛 `Exception("xxx")`，调用方无法精确区分错误类型 |
| Enum vs 字符串常量 | Enum 有类型检查，IDE 有自动补全，不会打错字 |

---

## 任务 1.4：建立配置管理——Settings 和 .env

**目标**：实现类型安全的配置管理。

**交付物**：
- `src/erp_copilot/infrastructure/config.py`（Pydantic Settings）
- `tests/unit/infrastructure/test_config.py`

**验收标准**：
- 缺失必填环境变量时启动报错（fail-fast）
- 每个配置项有类型、默认值和描述
- 敏感信息（密码、密钥）不会在日志中打印

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Pydantic Settings | 从环境变量/.env 加载配置并自动校验类型 |
| fail-fast 原则 | 配置有问题立刻崩溃，比运行到一半才发现好得多 |
| SecretStr 类型 | 打印时自动隐藏，防止日志泄露密钥 |

---

## 任务 1.5：Docker Compose 开发环境

**目标**：一键启动所有基础设施。

**交付物**：
- `infra/docker-compose.yml`（PostgreSQL + pgvector + Redis）
- `.env.example`（环境变量模板）

**验收标准**：
- `docker compose -f infra/docker-compose.yml up -d` 启动成功
- `docker compose ps` 所有服务 healthy
- `docker compose down -v` 清理干净

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Docker Compose | 用 YAML 定义多个容器，一条命令启动整个开发环境 |
| pgvector | PostgreSQL 扩展，让普通数据库也能存向量、做相似度搜索 |
| 为什么选 pgvector 而不是继续用 Milvus | 本地开发更简单，不需要额外服务；支持混合检索（全文+向量） |

---

## 任务 1.6：PostgreSQL + SQLAlchemy + Alembic 基础

**目标**：建立数据库连接和迁移基线。

**交付物**：
- `src/erp_copilot/infrastructure/database.py`（engine、SessionLocal、Base）
- `alembic.ini` 和 `migrations/` 目录
- `migrations/versions/001_baseline.py`（基线迁移）

**验收标准**：
- `alembic upgrade head` 创建表成功
- `alembic downgrade base` 回滚成功
- 迁移可重复执行（幂等性）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| SQLAlchemy | Python 最流行的 ORM，用 Python 对象操作数据库表 |
| ORM vs 原生 SQL | ORM 防注入、类型安全、易维护；原生 SQL 更灵活但易出错 |
| Alembic 迁移原理 | 每个迁移文件记录一次数据库变更（upgrade/downgrade），像 Git commit |
| sessionmaker | 创建数据库会话的工厂，每次请求一个会话，用完关闭 |

---

## 任务 1.7：核心数据模型——租户、用户、角色

**目标**：实现多租户用户系统的基础表。

**交付物**：
- `src/erp_copilot/domain/entities.py`（SQLAlchemy 模型：`Tenant`、`User`、`Role`、`RoleScope`）
- `migrations/versions/002_tenant_user_tables.py`
- `tests/unit/domain/test_user_entity.py`

**验收标准**：
- Tenant 和 User 之间有外键关系
- 同一 email 可在不同租户下注册
- Role 和 Scope 多对多关联

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 多租户（Multi-tenancy） | 一套系统服务多个客户（租户），数据互相隔离 |
| 外键（Foreign Key） | 数据库约束，保证引用的数据一定存在 |
| RBAC（Role-Based Access Control） | 基于角色的权限控制：用户→角色→权限范围 |

---

## 任务 1.8：核心数据模型——Tool 和 ToolVersion

**目标**：实现工具注册的数据表。

**交付物**：
- `src/erp_copilot/domain/entities.py`（新增 `Tool`、`ToolVersion`、`Parameter` 模型）
- `migrations/versions/003_tool_tables.py`

**验收标准**：
- Tool 和 ToolVersion 一对多关系
- Parameter 支持类型、是否必填、默认值
- Tool 有 `risk_level` 字段（READ/WRITE/ADMIN）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 工具版本（Immutability） | V5 中修改工具定义会影响历史数据；V6 中每次修改创建新版本，历史 Run 参数不变 |
| READ/WRITE/ADMIN 风险分级 | 不同操作需要不同权限：查询=READ，创建订单=WRITE，删除=ADMIN |

---

## 任务 1.9：核心数据模型——Run、RunStep、RunEvent

**目标**：实现 Agent 执行记录的数据表。

**交付物**：
- `src/erp_copilot/domain/entities.py`（新增 `Run`、`RunStep`、`RunEvent`）
- `migrations/versions/004_run_tables.py`

**验收标准**：
- Run 有状态字段（QUEUED→PLANNING→...→SUCCEEDED）
- RunStep 记录每个步骤的输入/输出/参数/耗时
- RunEvent 是 append-only 的事件日志

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Append-only 事件日志 | 只追加不修改，像银行流水——任何修改都有记录，可审计 |
| 乐观锁（version 字段） | 更新前检查版本号是否一致，防止两个 Worker 同时修改同一条数据 |

---

## 任务 1.10：FastAPI 应用工厂和健康检查

**目标**：搭建 FastAPI 应用骨架。

**交付物**：
- `apps/api/main.py`（应用工厂函数 `create_app()`）
- `apps/api/routes/health.py`（`GET /health` 端点）
- `tests/integration/test_health.py`

**验收标准**：
- `GET /health` 返回 `{"status": "ok"}` 并检查 PostgreSQL 和 Redis 连通性
- OpenAPI 文档自动生成（`/docs` 可访问）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 应用工厂模式 | `create_app()` 函数创建 FastAPI 实例，便于测试时用不同配置 |
| 为什么 FastAPI 不用 Flask | FastAPI 基于 Starlette（异步），自带数据校验（Pydantic），自动生成 API 文档 |
| async/await | Python 异步编程：等待 I/O 时不阻塞线程，可以同时处理其他请求 |

---

## 任务 1.11：Redis + Celery 集成

**目标**：实现异步任务投递和执行。

**交付物**：
- `apps/worker/celery_app.py`（Celery 实例配置）
- `src/erp_copilot/infrastructure/celery_config.py`
- `tests/integration/test_celery.py`

**验收标准**：
- Celery Worker 能成功启动并连接到 Redis
- 投递一个简单任务，Worker 能接收并执行
- 任务结果可以从 Redis 获取

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Celery 是什么 | Python 分布式任务队列：API 投递任务→Redis 暂存→Worker 取出执行 |
| 为什么需要 Celery | Agent 任务可能跑几分钟，不能在 HTTP 请求线程里等——那会超时 |
| Broker vs Backend | Broker（Redis）传递任务消息；Backend 存储任务结果 |
| V5 ThreadPoolExecutor 的问题 | 线程在进程内，进程重启任务丢失；Celery 任务持久化到 Redis，Worker 重启也不丢 |

---

## 任务 1.12：CI/CD 骨架（GitHub Actions）

**目标**：每次 push 自动运行 lint、类型检查和测试。

**交付物**：
- `.github/workflows/ci.yml`

**验收标准**：
- Push 触发自动运行 ruff、mypy、pytest
- 任何一项失败 CI 变红

---

# 阶段二：工具注册与模拟器（预计 10-12 个任务）

> **阶段教学引言**
>
> Agent 不是魔法——它不能"自动知道"怎么调 API。我们需要先告诉它：
> 有哪些工具可用、每个工具的参数是什么、工具的风险等级如何。
>
> 这个阶段你会学到：
> - **OpenAPI 3.x**：描述 REST API 的国际标准，Swagger 文档用的就是它
> - **MCP 协议**：Anthropic 提出的 AI 工具调用协议，让 LLM 以统一方式发现和调用工具
> - **ERP Simulator**：一个"假"的 ERP 系统，不用真的对接企业系统就能开发和测试
> - **幂等键（Idempotency Key）**：防止重复创建订单的关键机制

---

## 任务 2.1：Tool Registry——工具注册核心逻辑

**目标**：实现工具的创建、查询、版本管理。

**交付物**：
- `src/erp_copilot/tools/registry.py`（ToolRegistry 类）
- `tests/unit/tools/test_registry.py`

**验收标准**：
- 注册新工具→返回 Tool 对象
- 按名称查询→返回最新版本
- 按 ID 和版本号查询→返回指定版本
- 注册同名工具→自动创建新版本

---

## 任务 2.2：OpenAPI 3.x 导入器——解析

**目标**：从 OpenAPI JSON 文件解析工具定义。

**交付物**：
- `src/erp_copilot/tools/openapi_importer.py`（解析器）
- `tests/unit/tools/test_openapi_importer.py`

**验收标准**：
- 能正确解析 V5 的 `dataset_apis_aliyun.json`（25 个工具）
- 错误/不规范的 Schema 给出明确错误信息
- 路径参数、查询参数、请求体参数都能正确识别

---

## 任务 2.3：OpenAPI 3.x 导入器——校验和入库

**目标**：将解析后的工具定义校验并存入数据库。

**交付物**：
- `src/erp_copilot/tools/openapi_importer.py`（补充校验逻辑）
- `src/erp_copilot/application/tool_service.py`（ToolService 用例）

**验收标准**：
- 参数类型不合法→拒绝导入
- 缺少必填字段→拒绝导入
- 合法定义→写入 Tool + ToolVersion 表

---

## 任务 2.4：MCP Gateway——基础连接

**目标**：建立持久 MCP 连接，不再每次调用都重新启动进程。

**交付物**：
- `apps/mcp_gateway/gateway.py`（MCP Gateway 入口）
- `src/erp_copilot/tools/mcp_gateway.py`（连接管理、工具缓存）

**验收标准**：
- Gateway 启动后维持持久连接
- 连续两次调用不重新建立连接
- 断连后能自动重连

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| MCP 协议原理 | Client-Server 模式：Client（Agent）→请求工具列表→Server（Gateway）返回→Client 调用工具 |
| V5 的 MCP 问题 | 每次调用启动新 Python 子进程：启动 1s + 初始化 0.5s + 实际调用 0.1s = 浪费 |
| 持久连接优势 | 一次连接，多次调用，延迟降低 10 倍以上 |

---

## 任务 2.5：MCP Gateway——统一 ToolResult

**目标**：无论底层工具用 HTTP、gRPC 还是 MCP，上层收到的结果格式一致。

**交付物**：
- `src/erp_copilot/tools/tool_result.py`（`ToolResult`、`ToolError` 数据类）

**验收标准**：
- 成功结果包含 `data`、`status`、`elapsed_ms`
- 失败结果包含 `error_code`、`error_message`、`is_retryable`
- 大结果（>100KB）自动写入 Artifact

---

## 任务 2.6：ERP Simulator——商品和库存

**目标**：实现模拟 ERP 的商品查询和库存查询。

**交付物**：
- `apps/erp_simulator/simulator.py`（FastAPI 应用）
- `apps/erp_simulator/data/products.py`（商品种子数据）
- `apps/erp_simulator/routes/products.py`

**验收标准**：
- `GET /products/{name}` 返回商品信息（名称、价格、库存、单位）
- `GET /products/{id}/stock` 返回当前库存量
- 查询不存在的商品返回 404

---

## 任务 2.7：ERP Simulator——供应商

**目标**：实现供应商查询接口。

**交付物**：
- `apps/erp_simulator/data/suppliers.py`
- `apps/erp_simulator/routes/suppliers.py`

**验收标准**：
- 按区域查询供应商→返回该区域供应商列表
- 按状态筛选（可用/不可用）
- 返回费用、时效、评分

---

## 任务 2.8：ERP Simulator——订单读写

**目标**：实现订单创建和查询接口。

**交付物**：
- `apps/erp_simulator/routes/orders.py`

**验收标准**：
- `POST /orders` 创建订单，返回订单 ID
- `GET /orders/{id}` 查询订单状态
- 创建订单需要幂等键，重复请求返回相同结果
- 订单创建前校验库存

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 幂等键（Idempotency Key） | 客户端生成唯一 key，服务端记录：相同 key→返回缓存结果，不重复执行 |
| 为什么需要幂等 | 网络超时时客户端不知道服务端是否已处理，重复创建就是两个订单 |
| 幂等键生成策略 | UUID 或 hash(操作+参数) |

---

## 任务 2.9：ERP Simulator——场景控制

**目标**：通过 `scenario_id` 切换确定性场景。

**交付物**：
- `apps/erp_simulator/scenarios.py`（场景管理器）

**验收标准**：
- `scenario=stock_insufficient`：库存查询返回不足
- `scenario=supplier_unavailable`：供应商全部不可用
- `scenario=timeout`：订单创建模拟超时
- `scenario=happy_path`：所有操作正常

---

## 任务 2.10：API 路由——工具导入

**目标**：在 FastAPI 中暴露工具导入接口。

**交付物**：
- `apps/api/routes/tools.py`（`POST /v1/tools/import/openapi`、`GET /v1/tools`）

**验收标准**：
- 上传 OpenAPI JSON→工具入库
- 查询工具列表→返回工具名、版本、风险级别
- Schema 校验失败→返回 422 和具体错误

---

## 任务 2.11：API 路由——创建 Run（最小版本）

**目标**：实现最简的 Agent Run 提交接口。

**交付物**：
- `apps/api/routes/runs.py`（`POST /v1/runs`、`GET /v1/runs/{run_id}`）
- `apps/api/schemas/runs.py`（Pydantic 请求/响应模型）

**验收标准**：
- `POST /v1/runs` 返回 202 和 `run_id`
- Run 状态从 QUEUED 开始
- `GET /v1/runs/{run_id}` 返回当前状态

---

## 任务 2.12：第一个端到端集成测试

**目标**：验证 API→Celery→Simulator 链路。

**交付物**：
- `tests/e2e/test_happy_path.py`（使用 testcontainers）

**验收标准**：
- 提交一个查询商品的任务→Worker 执行→结果正确
- 超时场景下任务正确标记失败

---

# 阶段三：知识检索（预计 14 个任务）

> **阶段教学引言**
>
> Agent 不能只靠工具描述找到正确工具——它还需要理解业务上下文。
> 比如用户说"我想订苹果"，Agent 需要知道"苹果是一种水果，当前季节可能缺货"。
> 这些知识存储在知识库中，Agent 需要**检索**出最相关的内容。
>
> 这个阶段你会学到：
> - **知识分层**：规则类知识（L1 Skill，确定性注入）与描述类知识（L2 RAG，语义检索）分开
> - **向量检索（Vector Search）**：把文本转成数字向量，语义相近的向量距离近
> - **全文检索（Full-Text Search）**：传统搜索引擎用的关键词匹配
> - **混合检索（Hybrid Search）**：向量 + 全文，取两者之长
> - **Rerank（重排序）**：用更强的模型对检索结果精排
> - **Recall@K、MRR、NDCG**：检索系统的量化评价指标

---

## 任务 3.1：建立统一事实表

**目标**：先定义模拟器、知识库和评测集共享的"世界状态"，避免不同文档互相冲突。

**交付物**：
- `datasets/knowledge/manifest.yaml`（事实表，覆盖产品、供应商、订单字段和枚举、业务约束、审批要求）
- `tests/unit/retrieval/test_fact_table.py`（校验事实表字段与模拟器数据一致）

**验收标准**：
- 每个业务实体有字段定义、必填参数、API 依赖、业务约束、权限范围和审批要求
- 事实表与 ERP Simulator 种子数据一致（产品单位、库存、供应商区域、订单状态机）
- 评审：状态机和枚举无冲突

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 事实表（Source of Truth） | 一份统一定义，模拟器、知识库、评测集都引用它，避免三处口径不一致 |

---

## 任务 3.2：定义 L1 Skill 和意图→Skill 映射表

**目标**：把能写成"如果 A 则 B"的规则提取为结构化 Skill，由意图精确触发，确定性注入。

**交付物**：
- `datasets/knowledge/skills/skills.yaml`（10-15 个 Skill：状态机、参数约束、审批策略）
- `datasets/knowledge/skills/intent_skill_map.yaml`（`(domain, action)` → skill 列表）
- `src/erp_copilot/retrieval/skill_matcher.py`（确定性匹配，不走向量检索）
- `tests/unit/retrieval/test_skill_matcher.py`

**验收标准**：
- 每个意图 `(domain, action)` 精确命中 1-4 个 Skill，无误匹配
- 订单状态机、region 枚举约束、审批策略都在 L1 中定义
- 这些知识不进入 L2 检索

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 确定性 vs 概率 | Skill 匹配是精确查找（100% 命中），RAG 是语义召回（概率命中）。安全关键路径用确定性 |

---

## 任务 3.3：文档导入——Markdown 和文本解析

**目标**：上传知识文档，解析并分块。

**交付物**：
- `src/erp_copilot/retrieval/ingestion.py`（文档解析和分块器）
- `tests/unit/retrieval/test_ingestion.py`

**验收标准**：
- Markdown 正确解析（保留标题层级）
- 纯文本正确分块（默认 500 字/块，重叠 50 字）
- 每个 Chunk 包含元数据（来源文档、页码/章节）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Chunk（分块）是什么 | 长文档切成小块，每块是独立的检索单元 |
| 为什么需要重叠 | 关键信息可能正好跨在两个块的边界，重叠避免信息断裂 |
| Markdown 解析 | 利用标题层级（H1/H2/H3）保持结构信息，提升检索准确性 |

---

## 任务 3.4：文档导入——去重和版本管理

**目标**：同一文档重复上传不会创建重复数据。

**交付物**：
- `src/erp_copilot/retrieval/ingestion.py`（补充去重逻辑）
- `src/erp_copilot/domain/entities.py`（新增 KnowledgeDocument、DocumentChunk）

**验收标准**：
- 相同内容 hash 的文档→跳过
- 更新文档→创建新版本
- 导入任务有状态（PENDING/RUNNING/COMPLETED/FAILED）

---

## 任务 3.5：Embedding——文本转向量

**目标**：调用 Embedding 模型将文本块转成向量。

**交付物**：
- `src/erp_copilot/infrastructure/embedding.py`（Embedding 适配器接口 + 实现）

**验收标准**：
- 输入一段文本→返回 1024 维向量
- 相似文本向量余弦相似度高（>0.8）
- 不相似文本向量余弦相似度低（<0.5）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Embedding 是什么 | 把文字变成数字向量的技术，"苹果"→[0.1, 0.3, ..., -0.2] |
| 为什么用余弦相似度 | 衡量两个向量方向的一致性，不受向量长度影响 |
| 1024 维的含义 | 每个维度代表一个语义特征，维度越高表示能力越强（但也越慢） |

---

## 任务 3.6：pgvector——向量存储和检索

**目标**：将向量存入 pgvector 并实现基础向量搜索。

**交付物**：
- `src/erp_copilot/infrastructure/vector_store.py`
- `src/erp_copilot/retrieval/vector_search.py`

**验收标准**：
- Chunk 向量写入 pgvector
- 按查询文本搜索→返回 Top-K 最相似的 Chunk
- 搜索延迟 < 100ms（1000 条数据）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| pgvector 索引类型 | IVFFlat（快但近似）vs HNSW（更准但慢），我们默认用 IVFFlat |
| Top-K 是什么 | 检索返回最相似的 K 条结果，K 太大会引入噪音，太小可能漏掉答案 |

---

## 任务 3.7：全文检索——PostgreSQL FTS

**目标**：实现基于关键词的全文检索。

**交付物**：
- `src/erp_copilot/retrieval/fulltext_search.py`

**验收标准**：
- 搜索"苹果"→返回包含"苹果"的文档
- 搜索"苹果 库存"→返回同时包含两个词的文档（AND 逻辑）
- 中文分词正常工作

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 全文检索 vs LIKE | LIKE '%苹果%' 是全表扫描，全文检索有倒排索引，快 100 倍以上 |
| 倒排索引原理 | 一个词→包含它的所有文档 ID 列表，搜索时取交集 |
| 中文分词 | 中文没有空格，需要分词器把"苹果库存管理"切成"苹果/库存/管理" |

---

## 任务 3.8：混合检索——RRF 融合

**目标**：将向量检索和全文检索结果合并排序。

**交付物**：
- `src/erp_copilot/retrieval/hybrid_search.py`

**验收标准**：
- 向量结果和全文结果合并
- 融合后的排序优于单独任一种（人工判断）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| RRF（Reciprocal Rank Fusion） | 一种无需调权的融合算法：分数 = Σ 1/(k+rank)，向量排名和全文排名同权重 |
| 为什么不用加权求和 | 向量分数和全文分数不在同一尺度，很难公平加权 |
| 混合检索的优势 | 向量擅长语义匹配，全文擅长精确匹配，互补 |

---

## 任务 3.9：Rerank——Cross-Encoder 重排序

**目标**：对混合检索结果用更强的模型精排。

**交付物**：
- `src/erp_copilot/retrieval/reranker.py`

**验收标准**：
- 传入查询和候选文档列表→返回重排序后的列表
- Rerank 后 Top-5 的相关性优于不 Rerank

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Rerank 是什么 | 粗排（Bi-Encoder）快速召回候选→精排（Cross-Encoder）仔细打分 |
| Bi-Encoder vs Cross-Encoder | Bi-Encoder 分别编码查询和文档（快但粗糙）；Cross-Encoder 把查询和文档拼在一起编码（慢但准确） |
| V5 为什么也有 Rerank | V5 用 Qwen gte-rerank-v2，V6 继承这个两阶段策略 |

---

## 任务 3.10：引用装配和上下文装配

**目标**：检索结果带上引用来源，装配成 Agent 可用的上下文。

**交付物**：
- `src/erp_copilot/retrieval/context_assembler.py`

**验收标准**：
- 每条引用包含来源文档名、章节、页码
- 上下文按 token 预算截断
- 检索分数在引用中保留

---

## 任务 3.11：知识库 API 路由

**目标**：暴露知识导入和检索接口。

**交付物**：
- `apps/api/routes/knowledge.py`（`POST /v1/knowledge/ingest`）
- `apps/api/schemas/knowledge.py`

**验收标准**：
- 提交 Markdown 文本→创建导入任务
- 查询导入任务状态→返回进度
- 传入查询文本→返回检索结果（带引用）

---

## 任务 3.12：检索评测——40 条 RAG + L1 遵循

> **状态：✅ 已完成**（2026-08-07，Phase 3 收尾）

**目标**：建立 L2 RAG 检索评测数据集，并为 L1 Skill 建立遵循评测。

**交付物**（实际落地）：
- `datasets/eval/retrieval_queries.yaml`（42 条查询+标注的相关文档 ID，即任务原定的 retrieval_40.json，格式为 YAML、字段为 `relevant_docs`）
- `evals/datasets/skill_follow_20.json`（20 条 L1 遵循评测：给定意图，检查 LLM 是否遵循注入的约束）
- `evals/scorers/retrieval_scorer.py`（输出 Recall@1、Recall@5、MRR、NDCG@5、Precision@5、P50/P95 延迟，指标算法与 `evals/scripts/run_ablation.py` 逐位一致）
- `evals/scorers/skill_scorer.py`（输出约束遵循率、误触发率）

**验收标准**（落地核对）：
- ✅ 每条数据有 query、relevant_docs（相关文档 ID 为 `source` 字段值，与消融报告的 doc 级评测口径一致）
- ⚠️ 混淆负向：42 条查询未显式标注 negative 字段；消融逐查询分析中混淆度靠相关文档集覆盖（如"苹果的库存"相关 3 份文档）体现。若需要显式 hard negatives 评测，列为后续增量
- ✅ L1 遵循评测包含非法状态转换、非法 region 值等必须拒绝的场景（`tests/unit/evals/test_skill_follow_dataset.py::TestDatasetConsistency::test_includes_mandatory_reject_scenarios` 钉死，15 REJECT / 5 FOLLOW）
- ✅ 评分器输出 Recall@1、Recall@5、MRR、NDCG

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Recall@K | Top-K 结果中包含了多少正确答案：答对/应有。越高越好 |
| MRR | 第一个正确答案的排名的倒数，越靠前分数越高 |
| NDCG | 考虑排名位置权重的评价指标，排名越靠前权重越大 |
| L1 遵循率 | 注入的约束被 LLM 遵守的比例，不测召回（确定性注入命中 100%） |

---

## 任务 3.13：检索消融实验

**目标**：对比 Vector-only、Hybrid、Hybrid+Rerank 三组效果，并对比 L1 Skill 开关。

**交付物**：
- `evals/scripts/run_ablation.py`

**验收标准**：
- 一组命令运行三种配置并输出对比表格
- 每种配置跑完 40 条 RAG 评测集
- 额外跑 L1 Skill 开/关对比，验证确定性注入的增益

---

## 任务 3.14：检索优化迭代

**目标**：根据消融实验优化检索参数。

**交付物**：
- Chunk 大小、重叠、Top-K、RRF k 值调优

**验收标准**：
- 优化后有可测量的提升

---

# 阶段四：Agent Runtime（预计 16 个任务）

> **阶段教学引言**
>
> 这是项目的核心——Agent 如何把用户的自然语言请求，变成一系列有序的工具调用。
>
> V5 用的是顺序 while 循环：选工具→提取参数→调用→看结果→决定下一步。
> 这在工作流简单时够用，但面对复杂任务（先查库存、再比供应商、最后下单），
> 需要更结构化的方法。
>
> 这个阶段你会学到：
> - **LangGraph**：用状态图（Graph）来建模 Agent 行为，每个节点是一个操作
> - **Plan DAG**：有向无环图，描述步骤间的依赖关系
> - **Checkpoint**：保存执行进度，进程崩溃后能恢复
> - **Planner/Executor/Verifier 三角色**：各司其职，避免 LLM "越权"

---

## 任务 4.1：定义 Typed AgentState

**目标**：用 Pydantic 定义强类型的 Agent 状态。

**交付物**：
- `src/erp_copilot/agent/state.py`

**验收标准**：
- AgentState 包含 run_id, query, plan, current_step, status 等
- 状态可序列化/反序列化（用于 checkpoint）
- 类型检查通过

---

## 任务 4.2：搭建 LangGraph 状态图骨架

**目标**：创建 LangGraph 图，连接所有节点（先空实现）。

**交付物**：
- `src/erp_copilot/agent/graph.py`

**验收标准**：
- 图包含所有 9 个节点
- 边定义了合法的状态迁移
- 图可以编译并可视化（mermaid 输出）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| LangGraph 是什么 | LangChain 出的 Agent 框架，用图（Graph）定义状态机 |
| 节点（Node） | 图中的每一步操作：分类意图、检索、规划、执行、验证 |
| 边（Edge） | 节点之间的流转规则：A完成→B或C |
| 条件边（Conditional Edge） | 根据状态选择下一个节点：审批通过→执行，拒绝→结束 |

---

## 任务 4.3：classify_intent 节点

**目标**：判断用户意图类型、业务域和初始风险。

**交付物**：
- `src/erp_copilot/agent/nodes/classify_intent.py`

**验收标准**：
- 输入用户查询→输出意图分类（产品查询/供应商查询/订单创建/订单更新）
- 提取显式实体（产品名、数量、区域）
- 不生成工具参数（那是 Planner 的事）

---

## 任务 4.4：retrieve_context 节点

> **状态：✅ 已完成**（2026-08-07，L2 混合检索 + 意图构造查询 + 租户透传 + 引用保存）

**目标**：根据意图从知识库检索相关业务规则（L2 RAG）。

**交付物**：
- `src/erp_copilot/agent/nodes/retrieve_context.py`

**验收标准**：
- **必走节点**：每个 Run 都执行，LLM 不决策"是否检索"
- 调用混合检索获取相关文档
- 结果保存到 AgentState.retrieved_context
- 记录引用来源
- 对检索结果执行租户、ACL 和可信度过滤
- 根据意图构造查询（实体、业务域），不是把用户原话直接扔进向量库

---

## 任务 4.5：工具候选过滤

> **状态：✅ 已完成**（2026-08-07，Phase 3 收尾落地，早于任务 4.6 build_plan 使用）

**目标**：实现第一级意图→域确定性过滤，预留第二级向量精排接口。

**交付物**：
- `src/erp_copilot/tools/candidate_filter.py`（DOMAIN_TOOL_MAP + should_use_tool_retrieval 判定）
- `tests/unit/tools/test_candidate_filter.py`

**验收标准**（落地核对）：
- ✅ 主写意图 `(order/create, product/query, order/cancel)` 精确映射 3-8 个候选（测试钉死）；`supplier/query` 恰为 V6 注册的 2 个供应商工具；`security/data_access`、`system/scenario` 无工具候选（空列表）
- ⚠️ V5 25 工具基线：V6 工具集精简为 9 个（见 docs/05 工具表），映射表与 V5 不再一一对应；V5 意图训练数据转 40 条 Tool 检索评测 Case 列为后续增量
- ✅ 预留向量精排接口（`should_use_tool_retrieval` 严格大于阈值 5 才启用第二级），当前 9 个工具任何意图均不触发（`test_current_v6_intents_never_trigger_rerank` 钉死）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 候选过滤 vs 工具检索 | 意图过滤是确定性主引擎（3 选 1）；向量检索是概率精排（25 选 5），仅在候选过多时启用 |
| V5 工具检索的去向 | 两阶段思想保留为增长路径，角色从"唯一引擎"降级为"按需精排层" |

---

## 任务 4.6：build_plan 节点

**目标**：LLM 生成结构化的执行计划（Plan DAG），输入被约束为小候选集 + L1/L2 注入。

**交付物**：
- `src/erp_copilot/agent/nodes/build_plan.py`

**验收标准**：
- 输入查询和上下文→输出 Plan DAG JSON
- 每个 Step 有 tool_name、arguments、depends_on
- Planner 只输出 Plan，不直接调用工具
- **工具候选约束**：收到的工具是意图过滤后的 3-8 个（来自 DOMAIN_TOOL_MAP），不是全部工具
- **L1/L2 注入顺序**：System → L1 Active Skills（硬约束）→ L2 Retrieved Knowledge（参考）→ 候选工具 → User Query
- 检测 L1 约束被违反时（如非法状态转换），直接拒绝生成对应 Step

> **状态：✅ 已完成**（2026-08-08，LLM Plan DAG 生成 + System→L1→L2→候选→Query 注入顺序 + 严格 Schema 解析 + L1 非法转换拒绝；LLM 以 callable 注入，candidate_filter/skill_matcher 首次接线进图）

---

## 任务 4.7：validate_plan 节点

**目标**：校验 Plan DAG 的合法性（四层防御的第 2 层，确定性校验）。

**交付物**：
- `src/erp_copilot/agent/nodes/validate_plan.py`

**验收标准**：
- 检测循环依赖→拒绝
- 检测不存在的工具名→拒绝
- 检测缺少必填参数→拒绝
- 检测 argument_sources 引用的 Step 是否在 depends_on 中→拒绝
- 检测写 Step 是否有补偿/回退说明→拒绝
- 计算拓扑序和可并行步骤集合
- 放行但语义错选（如选了 getProductById 传 id=苹果）由第 4 层 verify_results 兜底

> **状态：✅ 已完成**（2026-08-08，Kahn 拓扑排序 + 环/未知工具/缺参/断依赖/写无回退五类确定性拒绝 + 并行分组输出 `PlanValidation`；工具 schema 注入，`validate_plan` 首次接线进图）

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| DAG 是什么 | 有向无环图，边有方向，不能形成循环（A→B→C→A 不行） |
| 拓扑排序 | 把 DAG 节点排成线性序列，保证依赖项在前面 |
| 为什么需要 validate | LLM 可能"幻想"出不存在的工具或参数，必须拦截 |
| 四层防御 | 候选约束（上游）→ validate_plan → Gateway 校验 → verify_results。单层无法防语义错选 |

---

## 任务 4.8：policy_check 节点

**目标**：检查计划中的每一步是否有权限执行。

**交付物**：
- `src/erp_copilot/agent/nodes/policy_check.py`

**验收标准**：
- READ 步骤→ALLOW
- WRITE 步骤、用户无 Scope→DENY
- WRITE 步骤、用户有 Scope→REQUIRE_APPROVAL
- ADMIN 步骤→REQUIRE_APPROVAL

> **状态：✅ 已完成**（2026-08-08，Scope 门禁先行（无 Scope 一律 DENY，审批不能授予权限）→ READ 放行 / WRITE·DANGEROUS·显式 requires_approval 需审批；`PolicyDecision` 写入 state，被拒步骤追加 `POLICY_DENIED` 错误；scope 解析以 callable 注入，`policy_check` 首次接线进图）

---

## 任务 4.9：execute_ready_steps 节点——基础版

**目标**：执行依赖已满足的步骤（先做串行版）。

**交付物**：
- `src/erp_copilot/agent/nodes/execute_steps.py`

**验收标准**：
- 按拓扑序依次执行 READ 步骤
- 调用 MCP Gateway 执行工具
- 结果写入 AgentState

> **状态：✅ 已完成**（2026-08-08，串行按 `plan_validation.topological_order` 执行 + `user_query`/`step:{id}` 参数运行时解析 + 依赖未 COMPLETED / 策略非 ALLOW 步骤跳过 + 失败→FAILED+StateError + 合并已有 step_results；executor 以 `(tool_name, arguments) -> ToolResult` callable 注入，`execute_ready_steps` 首次接线进图；4.10 起 executor 升级为 async，移除"同步适配器"设想，以本行描述为演进前基线）

---

## 任务 4.10：execute_ready_steps——并行版

**目标**：无依赖关系的 READ 步骤并行执行。

**交付物**：
- `src/erp_copilot/agent/nodes/execute_steps.py`（补充 asyncio.gather）

**验收标准**：
- 不互相依赖的步骤同时执行
- 并行结果正确合并（不依赖完成顺序）
- WRITE 步骤不参与并行

> **状态：✅ 已完成**（2026-08-08，executor 契约升级为 async `(tool_name, arguments) -> Awaitable[ToolResult]`（MCP Gateway 直接注入，移除同步适配器设想）；按 `plan_validation.parallel_groups` 逐波执行，波内 READ 步骤经 `asyncio.gather` 并发 await，WRITE 单例波串行不重叠；`parallel_groups` 为空时回退 `topological_order` 串行；失败/解析失败/异常隔离在波内不影响兄弟步骤）

---

## 任务 4.11：verify_results 节点

**目标**：检查执行结果是否满足业务目标。

**交付物**：
- `src/erp_copilot/agent/nodes/verify_results.py`

**验收标准**：
- 所有步骤 success→进入 finalize
- 有步骤 failed、是瞬态错误→进入 retry
- 有步骤 failed、是永久错误→进入 recover_or_replan
- 区分"工具调用成功"和"业务目标达成"

> **状态：✅ 已完成**（2026-08-08，`verify_results` 语义门（第 4 层防御）：COMPLETED 步骤按 `success_condition` 确定性求值（AST 白名单安全 eval，仅放行比较/布尔/算术表达式、属性与下标访问、`len/str/int/float` 调用 + 空 `__builtins__`，拒绝 `__import__`/任意调用，零新依赖），`response` 绑定结果数据；条件不满足→`SUCCESS_CONDITION_FAILED`、条件非法→`SUCCESS_CONDITION_INVALID`、步骤无结果→`MISSING_STEP_RESULT`（均永久）；FAILED 步骤按 `is_retryable` 归类（execute 已记 error 不重复追加）；SKIPPED 不判失败。分类写入 AgentStatus：无失败→SUCCEEDED（finalize）、全部可重试→RETRYING、任一永久→REPLANNING——9 节点拓扑只有 recover_or_replan 一个恢复汇点，retry/replan/give-up 三路拆分留给 5.8，路由仍由 errors 驱动；graph 增加 `verify_node` 注入参数，stub 改名 `_verify_results_noop`）

---

## 任务 4.12：Checkpoint 和恢复

**目标**：每个节点完成后保存状态，进程崩溃后能恢复。

**交付物**：
- `src/erp_copilot/memory/checkpoint.py`

**验收标准**：
- 每个节点执行后保存 checkpoint 到 PostgreSQL
- Worker 重启后从最新 checkpoint 恢复
- 已完成的步骤不会重复执行

> **状态：✅ 已完成**（2026-08-08，两个提交：① `CheckpointSaver` 原语——`agent_checkpoints` 表（迁移 d4e5f6a7b8c9：run_id/tenant_id/node_name/run_status/state_json/created_at），每次 save 立即 commit（崩溃恢复要求写先于下一步持久化），`load_latest(run_id, tenant_id)` 租户隔离恢复，`map_agent_status` 运行时→持久化状态映射（QUEUED/PLANNING→PENDING、EXECUTING/VERIFYING/RETRYING/REPLANNING→RUNNING、SUCCEEDED→COMPLETED、FAILED/EXPIRED→FAILED、CANCELLED→CANCELLED），`checkpointed()` 包装器保留 async 语义；② 图接线 + 幂等执行——`build_agent_graph(checkpoint_saver=...)` 统一包装全部 9 节点（节点注册重构为循环），execute_ready_steps 加恢复守卫：已有 COMPLETED 结果的步骤不重跑（FAILED 照常重试）。不用 LangGraph 自带 checkpointer（需未批准依赖 langgraph-checkpoint-postgres）；恢复 = 用最新状态重 invoke 图，无副作用节点重跑无害、工具调用被步骤级幂等拦截；测试用 SQLite 内存库（仅建 checkpoint 表，整库元数据含 TSVECTOR/pgvector 无法编译）+ 注入时钟保证排序确定性）

---

---

## 任务 4.13：上下文预算管理

**目标**：控制发送给 LLM 的总 token 数。

**交付物**：
- `src/erp_copilot/memory/context_budget.py`

**验收标准**：
- 系统指令 + Tool Schema + Plan + 知识 + 历史 + 输出 ≤ 预算
- 超预算时先压缩工具结果和历史
- 安全策略和审批信息不压缩

---

## 任务 4.14：SSE 事件流

**目标**：Agent 执行过程通过 SSE 实时推送给客户端。

**交付物**：
- `apps/api/routes/runs.py`（补充 `GET /v1/runs/{run_id}/events`）

**验收标准**：
- 客户端连接 SSE→收到 QUEUED→PLANNING→EXECUTING→...事件
- 客户端断开不影响 Worker
- 重连后可以收到后续事件

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| SSE（Server-Sent Events） | 服务端→客户端单向推送，比 WebSocket 简单，适合状态更新 |
| SSE vs WebSocket | SSE 单向、自动重连、走 HTTP；WebSocket 双向、更复杂 |
| 为什么不用轮询 | GET/status 每秒一次浪费资源；SSE 有事件才推送 |

---

## 任务 4.15：取消和超时

**目标**：支持取消正在执行的 Run 和设置截止时间。

**交付物**：
- `POST /v1/runs/{run_id}/cancel`
- AgentState.deadline_at 机制

**验收标准**：
- 取消请求→Worker 在下一个节点停止执行
- 超时→Run 状态变为 EXPIRED
- 取消/超时不产生脏数据

---

## 任务 4.16：首个只读场景端到端演示

**目标**：完成一个完整的只读任务（查库存+选供应商）。

**交付物**：
- 端到端测试：输入"查苹果库存并推荐供应商"→Agent 正确执行两个 READ 步骤

---

# 阶段五：安全、审批与恢复（预计 10-12 个任务）

> **阶段教学引言**
>
> 当 Agent 不仅能读数据还能写数据时，安全问题就至关重要了。
> 如果用户说"帮我订 100 吨苹果"，Agent 不应该直接下单——需要先确认库存、
> 验证权限、让用户审批。
>
> 这个阶段你会学到：
> - **五层安全防护**：从输入到输出的纵深防御
> - **SSRF 防护**：防止 Agent 被利用访问内网
> - **审批工作流**：高风险操作必须人工确认
> - **幂等重试**：保证"最多执行一次"的写操作语义

---

## 任务 5.1：RBAC 和 Tool Scope

**目标**：实现基于角色的工具权限控制。

**交付物**：
- `src/erp_copilot/security/rbac.py`
- `src/erp_copilot/security/dependencies.py`（FastAPI 依赖注入）

**验收标准**：
- 用户有 `supplier:read` Scope→可查询供应商
- 用户无 `order:write` Scope→创建订单被拒绝（403）
- 跨租户访问→403

---

## 任务 5.2：审批模型——暂停和决策

> **状态：✅ 已完成**（2026-08-08，暂停+决策+HTTP 路由+audit_log 全部落地）

**目标**：实现 WRITE 操作的审批暂停与决策。

**交付物**：
- `src/erp_copilot/security/approval.py`（新增，决策半程 + audit_log 落库 + `decide_and_resume` 编排）
- `src/erp_copilot/agent/nodes/request_approval.py`（新增，暂停半程）
- `src/erp_copilot/domain/entities.py`（新增 `AuditLog`）
- `migrations/versions/f7e8d9c0b1a2_add_audit_log_table.py`（新增）
- `apps/api/routes/runs.py` + `apps/api/schemas/runs.py`（新增 approve 路由与 `ApproveRunRequest`）
- `tests/integration/test_approval_api.py`（新增，4 用例）

**验收标准**：
- ✅ 高风险步骤→Run 进入 WAITING_APPROVAL（`request_approval_node`：REQUIRE_APPROVAL 步骤 → PENDING `ApprovalRequest` 记录 + `AgentStatus.WAITING_APPROVAL`，图路由到 END 暂停；`state.py` 新增 `ApprovalStatus`/`ApprovalRequest`/`AgentState.approvals`）
- ✅ 用户调用 approve API→继续执行（`POST /v1/runs/{run_id}/approve`：`ApprovalDecisionService.decide` 把 PENDING 翻成 APPROVED/DENIED 并落新 checkpoint；`decide_and_resume` 先决策再审计再 `resume_run` 重放恢复——`request_approval` 把已决记录解析回 policy：APPROVED→ALLOW 使步骤执行、DENIED→DENY 使步骤被跳过）
- ✅ 用户 deny→步骤被跳过，Run 进入对应状态（DENIED 记录 → policy DENY → execute_ready_steps 跳过；全跳过 → verify 判 SUCCEEDED）
- ✅ 审批记录写入 audit_log（`record_audit_log` 落 `AuditLog`：actor/resource/action/result/ip/trace_id；`created_at` 只 default 无 onupdate → 不可变时间戳；迁移 f7e8d9c0b1a2）

**落地细节**：节点为纯函数（无注入依赖、幂等追加不重复建记录）；路由基于**当前 plan 步骤**判断（`pending_approval_step_ids` 与节点共用单一来源），不依赖 `state.status`（resume 重放时 classify 会把 status 重置为 PLANNING），并能丢弃 replan 残留的陈旧 PENDING 记录；暂停路由到 END 而非 finalize（finalize 语义是持久化完结，WAITING_APPROVAL 不该被"完结"）。恢复 = checkpoint 重放 + 幂等（4.12 决策）。`ApprovalRequest` 新增 `decided_by`/`decided_at`/`reason` 审计字段。关键交互：resume 时 policy 会对同一 WRITE 步骤重判 REQUIRE_APPROVAL（scope 只判 DENY，不授予写权限），所以 `request_approval` 必须把已决记录解析回 policy，否则 execute 会把已批准步骤当 DENY 跳过。10 节点图拓扑（docs/03 §4），测试：`tests/unit/security/test_approval.py`（18，含新增 `TestAuditLog` 3 + `TestDecideAndResume` 2）+ `tests/unit/agent/test_request_approval.py`（14）+ `test_graph.py` 暂停/恢复路由（3）+ `tests/integration/test_approval_api.py`（4）。HTTP 路由：tenant 从 Run 行派生（不信任客户端，租户隔离）；错误映射 `NotFoundError`→404、其余 `CopilotError`→409；`build_agent_graph(checkpoint_saver=saver)` 建图（默认 no-op 注入节点，真实节点接线随 worker 接线任务落地）。

---

## 任务 5.3：SSRF 防护和 Egress 控制

> **状态：✅ 已完成**（2026-08-08，纯函数前置检查 + 注入式解析器 + security_events 落库）

**目标**：防止 Agent 访问内网或危险 URL。

**交付物**：
- `src/erp_copilot/security/ssrf_guard.py`（新增）
- `src/erp_copilot/domain/entities.py`（新增 `SecurityEvent`）
- `migrations/versions/a1b2c3d4e5f6_add_security_event_table.py`（新增）
- `tests/unit/security/test_ssrf_guard.py`（新增，33 用例）

**验收标准**：
- ✅ 请求内网 IP（127.0.0.1/10.x/192.168.x）→拦截（`is_internal_ip` 按族拆表：IPv4 的 0/8、RFC1918、回环、链路本地、组播、保留；IPv6 的 ::、::1、ULA、链路本地、组播；`BLOCKED_IP`）
- ✅ 请求非白名单域名→拦截（预注册 `allowed_hosts` 或 `host:port`；`HOST_NOT_ALLOWED`）
- ✅ 拦截事件写入 security_events（`record_security_event` 落 `SecurityEvent(attack_type="SSRF", layer="ssrf_guard", severity="HIGH", disposition="blocked")`）

**落地细节**：纯函数前置检查（`SSRFGuard.check`，不发连接）按序：scheme 白名单（仅 https）→ 拒绝 URL 凭证 → 端口白名单（默认 443，显式端口或 scheme 默认）→ host 白名单 → 解析后逐个地址检查（注入的 resolver 可确定性模拟 DNS rebinding；任一地址为内网即拦截；IP 字面量跳过 DNS；空解析 `RESOLUTION_FAILED` 失败关闭）。`trusted_internal_hosts` 显式授权合法内网端点（如 ERP simulator），但仍须在 allowed_hosts（纵深防御）。`check_redirect` 只允许同 host 重定向并逐跳复检。测试：33 用例覆盖 IPv4/IPv6 内外网、scheme、白名单粒度（裸 host vs host:port）、端口、DNS rebinding、重定向、凭证、非法 URL、trusted_internal。已知限制：TOCTOU——检查时解析与后续连接时的解析可能不一致（需 executor 直接连已校验地址才能闭合，超出本任务）；guard 到 executor/MCP gateway 的接线待续。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| SSRF 是什么 | Server-Side Request Forgery：攻击者让服务器请求内部服务 |
| Agent 场景的 SSRF 风险 | 恶意 prompt 可能让 Agent 调用 `http://internal-admin/delete-all` |
| 防护策略 | IP 黑名单 + 域名白名单 + URL scheme 限制（只允许 https） |
| DNS Rebinding | 解析返回全部地址并逐一校验，任何内网地址即拦截；纯函数 + 注入 resolver 让测试可确定性模拟 |
| Verdict 模式 | guard 返回 `SSRFVerdict(allowed, reason, detail)` 而非抛异常，处置权交给调用方（拦截 + 记 security_event） |

---

## 任务 5.4：Prompt Injection 防护

**目标**：检测和拦截恶意用户输入。

**交付物**：
- `src/erp_copilot/security/injection_guard.py`（✅ 已实现）
- `tests/unit/security/test_injection_guard.py`（✅ 已实现，21 用例）

**验收标准**：
- ✅ "忽略之前指令"→标记为可疑（`IGNORE_PRIOR_INSTRUCTIONS`）
- ✅ "跳过审批直接下单"→标记为可疑（`BYPASS_APPROVAL`）
- ✅ "泄露系统提示词"→标记为可疑（`LEAK_SYSTEM_PROMPT`）

**落地细节**：纯正则检测器（`InjectionGuard.check(text)`，确定性、无网络 I/O）复用 SSRF guard 的 verdict 模式——返回 `InjectionVerdict(flagged, matched_rules, detail)` 而非抛异常，处置权交给调用方（输入层 block、知识层 quarantine，见 docs/06 §7 的输入/知识双层区分）。三条默认规则覆盖任务验收的三大攻击族：`IGNORE_PRIOR_INSTRUCTIONS`（忽略之前指令/忽略以上所有指令/ignore all previous instructions/ignore the instructions above）、`BYPASS_APPROVAL`（跳过审批/绕过审批/绕过审核/bypass approval）、`LEAK_SYSTEM_PROMPT`（泄露/显示/出示系统提示词/reveal your system prompt），均 `re.IGNORECASE`。多规则命中时 `matched_rules` 按规则序全量报告，`detail` 取首条。`InjectionGuard(rules=[...])` 可用自定义规则替换默认集，`rules=[]` 永不标记（用于测试/禁用）。拦截事件经 `record_injection_event` 落 `SecurityEvent(attack_type="PROMPT_INJECTION", layer="injection_guard", severity="HIGH")`，`disposition` 默认 `blocked`、可传 `quarantined` 支持 RAG 知识层隔离。误报测试钉住正常 ERP 请求（"查询苹果的库存""创建订单，数量5，发往上海""请忽略发货延迟的情况"等）不得被标记；裸"忽略"不足以触发，必须命中完整攻击句式。guard 到 executor/MCP gateway 的接线待续。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Prompt Injection 是什么 | 攻击者把指令混进数据（用户输入/检索到的知识），让 Agent 执行非预期动作 |
| 输入层 vs 知识层 | 输入层直接 block；知识层（RAG Poisoning）无法拒绝数据，需 quarantine 隔离进上下文 |
| 确定性优先 | 用正则识别已知话术而非 LLM 判意——行为可审计、可测试、无随机性 |
| Verdict 模式 | guard 返回 `InjectionVerdict(flagged, matched_rules, detail)`，调用方决定处置 + 记 security_event，与 SSRF guard 一致 |
| 规则可注入 | `InjectionGuard(rules=[...])` 替换默认集，`rules=[]` 关闭检测；多规则命中全量报告 |

---

## 任务 5.5：Secret Redaction

**目标**：输出结果中自动隐藏密钥、密码等敏感信息。

**交付物**：
- `src/erp_copilot/security/redaction.py`（✅ 已实现）
- `tests/unit/security/test_redaction.py`（✅ 已实现，17 用例）

**验收标准**：
- ✅ API Key 模式→替换为 `[REDACTED]`（`API_KEY` 规则：`sk-`/`AKIA`/`ghp_` 前缀，`re.IGNORECASE`）
- ✅ 手机号/身份证号→部分隐藏（`CN_MOBILE` 保前3后4、`CN_ID_CARD` 保前6后4，中间 `*`）
- ✅ 不影响正常的业务数据（座机、订单号、SKU、17 位编号等误报用例全部原样通过）

**落地细节**：纯确定性输出转换器（`Redactor.redact(text)`，无网络、无 DB），与 SSRF/Injection guard 的关键区别是**它是 transformer 而非拦截器**——每次出站回答都会跑，原地改写文本，结果只带 `RedactionResult(redacted, count, matched_rules)` 交给调用方决定是否记录，因此模块框架无关、单测极简。规则沿用 `RedactionRule(name, pattern, replace)` 可注入模式：`replace` 是 `Callable[[re.Match[str]], str]`，API Key 用常量替换，PII 用 `_mask_keep_edges` 掩码。规则链式处理（每规则在上一规则输出上跑，避免二次掩码）；数字边界断言 `(?<!\d)...(?!\d)` 保证 17 位编号不误判为 18 位身份证、12 位订单号不误判为 11 位手机号。座机 `010-`（非 `1[3-9]` 开头）故意不隐藏，展示精确性。`Redactor(rules=[...])` 替换默认集，`rules=[]` 纯透传。Redaction 事件落库（记录"本次输出命中了哪些敏感类型"）留待安全事件审计任务，本任务不引入 DB 依赖。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Secret/PII Redaction 是什么 | 输出层把密钥、手机号、身份证号替换为占位或部分掩码，防止数据经 Agent 回答泄露 |
| transformer vs guard | guard 拦截攻击（返回 verdict + 落 security_event）；redaction 改写所有输出（纯转换，无 DB）——职责不同，落库选择留给调用方 |
| 部分隐藏 vs 全替换 | 密钥无保留价值→`[REDACTED]`；PII 需保留边缘供审计对账→`138****8000`/`110101********123X` |
| 数字边界断言 | `(?<!\d)...(?!\d)` 让正则只在完整数字串上匹配，长编号不误伤短格式 |
| 规则链式处理 | 每规则在上一规则输出上跑，`[REDACTED]` 不会被后续规则再次命中；`count` 累计 |

---

## 任务 5.6：幂等写操作

**目标**：保证同一个写操作不会被执行两次。

**交付物**：
- `src/erp_copilot/tools/idempotency.py`（✅ 已实现）
- `src/erp_copilot/domain/entities.py`（✅ `IdempotencyRecord` 实体）
- `src/erp_copilot/domain/errors.py`（✅ `IdempotencyConflictError`）
- `migrations/versions/6b5c4d3e2f10_add_idempotency_record_table.py`（✅ 新增表）
- `tests/unit/tools/test_idempotency.py`（✅ 已实现，8 用例）

**验收标准**：
- ✅ 相同幂等键→返回缓存结果（`execute` 短路径：`COMPLETED` 记录直接重放 `result_payload`，`fn` 不再执行）
- ✅ 不同幂等键→正常执行（各 key 独立走 begin→fn→complete，`count` 累计两条记录）
- ✅ 幂等记录持久化（PostgreSQL）（`idempotency_records` 表 + 迁移，`(tenant_id, idempotency_key)` 唯一约束）

**落地细节**：`IdempotencyStore(session)` 沿用 `CheckpointSaver(session)` 的注入风格，围绕 tool 执行编排 docs/06 §7 三规则——**执行前先写意图**（`begin` 插 `PENDING` 行）→ 执行 → **成功后记录结果**（`complete` 置 `COMPLETED` + `result_payload` + `external_operation_id`）；失败置 `FAILED` + `error_message`，允许后续重跑（覆盖为 COMPLETED）。`execute` 是编排入口：`begin` 返回 `COMPLETED` 即缓存短路径，否则跑 `fn`。并发安全由唯一约束兜底：store 记录本实例创建的 PENDING id，`begin` 遇到**自己创建的** PENDING 视为幂等重入返回同记录，遇到**他人创建**的 PENDING 抛 `IdempotencyConflictError`（在途冲突）；两个进程竞态绕过查询时，第二次插入撞唯一约束 `IntegrityError`→回滚→重查→同冲突或返回已 COMPLETED 记录。测试用共享 SQLite 文件 + 两个独立 session 验证跨 session 持久化（缓存真正从 DB 读回，而非同 session identity map）。状态语义对齐 docs/06 §8：COMPLETED 直接复用、FAILED 按预算重试、PENDING 对账转人工。request_payload 存执行意图供审计/对账，但**不做重放参数一致性校验**（同 key 不同参 → 后续增强）；executor 接线幂等层随 Worker 恢复任务。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 幂等键是什么 | 每次写操作生成的稳定标识，让"执行两次=执行一次"成为可能（docs/06 §7：Plan 确定后生成） |
| 执行意图 + 唯一约束 | 先插 `PENDING` 意图行，靠 DB 唯一约束拦截重放——防并发双写靠数据库而不是应用层 if |
| COMPLETED 缓存 vs FAILED 重跑 | 成功结果永久复用（缓存）；失败记录允许按预算重试（docs/06 §8 恢复语义） |
| 在途冲突 | `PENDING` 记录还在（进程崩溃/并发）→ `IdempotencyConflictError`，绝不盲目重跑 |
| 幂等 vs 重试的关系 | 幂等让"重试安全"成为可能——这正是 docs/06 §7"结果未知且无幂等保障的写操作不可自动重试"的前提 |

---

## 任务 5.7：退避重试策略

**目标**：瞬态错误自动重试，永久错误不重试。

**交付物**：
- `src/erp_copilot/agent/retry_policy.py`（✅ 已实现）
- `tests/unit/agent/test_retry_policy.py`（✅ 已实现，16 用例）

**验收标准**：
- ✅ 超时/429→指数退避重试（1s→2s→4s）（`RetryPolicy.delay_before_attempt` 精确序列 1.0→2.0→4.0；`RetryExecutor` 用注入的 fake sleep 断言）
- ✅ 4xx 业务错误→不重试（`is_retryable_http`：仅 429/5xx 可重试；executor 对 `is_retryable=False` 的 FAILED 一次都不重试、不 sleep）
- ✅ 重试次数在 Step 的 max_retries 范围内（`execute(fn, max_retries=2)` 共尝试 3 次后返回最终 FAILED，`max_retries=0` 单次）

**落地细节**：`RetryPolicy`（frozen dataclass）把 docs/06 §7 的重试规则落成确定性判定——`is_retryable_http`（429 限流 + 500-599 暂时性 5xx 可重试，其余 4xx 为永久业务错误）+ `is_retryable_exception`（ConnectionError/TimeoutError 是 OSError 子类，单条 `isinstance(exc, OSError)` 且非 FileNotFound/Permission/NotADirectory 即瞬态）+ `delay_before_attempt`（`min(base * 2**attempt, max_delay_s)`，上限 60s）。`RetryExecutor.execute(fn, max_retries)` 消费执行链路现有的 `ToolResult`（`error.is_retryable` 已由 `_to_step_result` 复制到 `StepResult`，verify_results 据此分类）——返回第一个 SUCCEEDED 或最终 FAILED，**不抛异常**，单个瞬态失败不杀掉整波执行。`sleep` 可注入（默认 `time.sleep`），测试用 `_FakeSleep` 断言精确退避序列 `[1.0, 2.0]`。重试预算来自 `PlanStep.max_retries`（state.py 已有字段）；Run deadline 与抖动（docs/06 提到但验收未要求）留待接线；`is_retryable_http`/`is_retryable_exception` 供执行器在构造 `ToolResult.failure(is_retryable=...)` 时复用，本任务不含 executor 接线（随 Worker 恢复任务）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 瞬态 vs 永久错误 | 连接重置/429/暂时 5xx/未执行超时可重试；鉴权失败/Schema 错误/业务拒绝/参数冲突不可重试（docs/06 §7 清单） |
| 指数退避 | `base * 2**attempt`：1s→2s→4s，加 `max_delay_s` 上限防失控；抖动留待接线防雷群效应 |
| 与幂等的关系 | 上一任务 5.6 的幂等保障正是"可安全重试"的前提——"结果未知且无幂等保障的写操作不可自动重试" |
| 返回结果而非抛异常 | 执行链路统一走 `ToolResult`（FAILED + is_retryable），verify_results 分支分类；retry 只是该链路的退避编排，不新增错误路径 |
| 可注入 sleep | `RetryExecutor(sleep=...)` 让测试断言精确延迟，无真实等待 |

---

## 任务 5.8：Worker 恢复流程

**目标**：Worker 崩溃重启后恢复执行。

**交付物**：
- `src/erp_copilot/agent/recovery.py`（✅ 已实现）
- `tests/unit/agent/test_recovery.py`（✅ 已实现，13 用例）

**验收标准**：
- ✅ Worker 重启→加载最新 checkpoint（`RunRecovery.load` 复用 `CheckpointSaver.load_latest`，按 `(run_id, tenant_id)` 取最新行；无 checkpoint → `resumed=False`；B 覆盖 A 由注入 clock 保证确定性；租户隔离：t2 查 t1 的 run → 不恢复）
- ✅ 状态不明确的写 Step→标记为需要对账（PENDING 幂等记录 → 追加 `StateError(code="RECOVERY_RECONCILIATION_REQUIRED", step_id, details={"idempotency_key"})`，`reconciled_steps` 记录；不盲目重试）
- ✅ 已完成的 Step→不重复执行（COMPLETED step result 或 COMPLETED 幂等记录都跳过；`step_results` 原样保留，`execute_ready_steps` 据此跳过）

**落地细节**：`RunRecovery(session, *, saver=None)` 是纯编排模块——注入 session（与 `CheckpointSaver`/`IdempotencyStore` 同风格），`saver` 默认构造 `CheckpointSaver(session)`；`load(run_id, tenant_id)` 返回 frozen dataclass `RecoveryResult(state, resumed, reconciled_steps)`，不持久化、不重存 checkpoint，由 Worker 把对账后的 state 重新喂回图，下一个节点的 checkpoint save 自然落盘新增的 error。`_reconcile(state)` 遍历 `plan.steps`，只关心 WRITE/DANGEROUS 且带 `idempotency_key` 的 Step，按 `(tenant_id, idempotency_key)` 查幂等记录（与 5.6 唯一约束一致）：**PENDING**（执行意图已写、结果未知）→ 标记对账；**COMPLETED**（已成功，checkpoint 只是滞后）→ 跳过；**FAILED**（明确失败、可重试）→ 跳过；**无记录**（`begin()` 从未跑过，工具必然未执行）→ 安全直接跑，跳过。优先级：checkpoint 里 COMPLETED 的 step result 胜过任何 PENDING 记录（已成功优先，防 stale 幂等行误判）。`RECOVERY_RECONCILIATION_REQUIRED` 是 docs/03 §9 错误码，落成模块常量供后续 Worker/对账服务引用；现有 error 保留（append 不覆盖）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 恢复五步（docs/03 §7） | 获锁 → 加载最新 checkpoint → 核对成功 Step + 幂等记录 → 不明确写 Step 标记对账而非直接重试 → 从下一个合法节点继续 |
| PENDING 幂等记录 = 状态不确定 | 5.6 的 `begin()` 先写执行意图再执行：PENDING 意味着"可能已执行、结果未知"，直接重试可能重复落库，故标记 `RECOVERY_RECONCILIATION_REQUIRED` |
| 已成功优先 | COMPLETED step result / COMPLETED 幂等记录都证明写已生效；对账时不得因 stale PENDING 行而误标记 |
| 纯编排 vs 持久化 | recovery 只读 checkpoint + 幂等记录并追加错误，不写库——避免恢复逻辑自己也成为崩溃点，落盘交给图的下一次 checkpoint |
| 租户隔离 | `load_latest` 带 tenant 过滤 + 幂等记录按 tenant 查，恢复永不跨界 |
| 与 5.6/5.7 关系 | 5.6 幂等是"能否安全重试"的判定依据，5.7 决定"可重试的怎么退避"，5.8 决定"状态不明的不重试先对账"——三层各管一段 |

---

## 任务 5.9：失败队列和人工处理

**目标**：无法自动恢复的失败进入人工处理队列。

**交付物**：
- `Run` 增加 `failure_code` / `failure_reason` / `suggested_action` 三列 + 迁移 `7c8d9e0f1a2b`（✅ 已实现）
- `src/erp_copilot/application/failure_queue.py`（✅ 已实现）
- `tests/unit/application/test_failure_queue.py`（✅ 已实现，11 用例）

**验收标准**：
- ✅ 永久失败→Run FAILED，记录具体错误（`FailureQueue.record(error_code="PERMANENT_TOOL_ERROR", reason, suggested_action)`：置 `status="FAILED"` + 三字段 + `completed_at` + `version+=1`）
- ✅ 对账失败→标记为需要人工介入（`record(error_code="RECOVERY_RECONCILIATION_REQUIRED", ...)` 落入 `list_needing_intervention`，即 5.8 的对账场景落地）
- ✅ 失败信息足够让开发者定位问题（机器可读 `failure_code` + 人类可读 `failure_reason` + `suggested_action` 落库，并追加不可变 `RUN_FAILED` 事件，payload 带全部三字段）

**落地细节**：`FailureQueue`（注入 session + 可注入 clock，与 CheckpointSaver/IdempotencyStore 同风格）暴露两个方法——`record(run_id, tenant_id, *, error_code, reason, suggested_action)` 按 `(id, tenant_id)` 加载（租户隔离：t2 不能 fail t1 的 run），COMPLETED/CANCELLED 拒绝翻转（`CopilotError(code="INVALID_RUN_TRANSITION")`，防把已成功 run 打成失败），否则置 FAILED + 三字段 + `completed_at` + `version+=1`，追加 `RUN_FAILED` 事件（`sequence` 取 `max+1`，docs/03 §3 要求状态变化必须追加不可变 run_event），提交；`list_needing_intervention(tenant_id)` 返回 `status=FAILED` 且 `failure_reason` 非空的 run，按 `completed_at` 倒序（可注入 clock 保证确定性），即"人工处理队列"。`error_code` 复用 docs/03 §9 错误码清单（PERMANENT_TOOL_ERROR / RECOVERY_RECONCILIATION_REQUIRED / LLM_OUTPUT_ERROR / DEADLINE_EXCEEDED...）。Run 锁/CAS 属 5.8 Worker 流程，本服务只做`version+=1`沿用既有惯例；Worker 接线（把 execute_run 的 FAILED 分支换成调用本服务）留给接线任务。迁移 `7c8d9e0f1a2b` 只加 3 个 nullable 列，down_revision=`6b5c4d3e2f10`（head）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 失败队列 = 查询面而非新表 | "队列"就是 `status=FAILED` 且带 `failure_reason` 的 Run 集合，按完成时间倒序——不加新表、不加后台进程，查就是队头 |
| 诊断三要素 | `failure_code`（机器可读、可路由）、`failure_reason`（人类可读、定位问题）、`suggested_action`（给操作员的下一步）——docs/03 §9 错误码清单是 code 的取值来源 |
| 状态机安全 | COMPLETED/CANCELLED 是终态，拒绝翻转（`INVALID_RUN_TRANSITION`），防止已成功 run 被误判失败；FAILED 可重录（补充详情） |
| 事件审计 | docs/03 §3 要求每次状态变化追加不可变 run_event——失败详情除了落在 run 行，还写进 `RUN_FAILED` 事件（`sequence` 单调递增），审计不依赖业务表 |
| 与 5.8 关系 | 5.8 把"状态不明的写 Step"标成 `RECOVERY_RECONCILIATION_REQUIRED`，本任务把它落成 Run FAILED + 人工介入标记——对账失败即入人工队列 |

---

## 任务 5.10：安全评测数据集（25 条）

**目标**：建立安全评测用例。

**交付物**：
- `evals/datasets/security_25.json`（✅ 已实现：25 条 = 10 注入/越权 + 5 SSRF + 5 秘密泄露 + 5 良性）
- `evals/scripts/run_security_eval.py`（✅ 已实现：确定性评测脚本）
- `tests/unit/evals/test_security_eval.py`（✅ 已实现，16 用例）

**验收标准**：
- ✅ 每条有攻击描述、期望拦截结果（每条含 `attack_type`/`target_layer`/`input`/`expected` + 可选 `note`，`expected` ∈ {BLOCK, ALLOW}）
- ✅ 计算攻击拦截率和误报率（`interception_rate = 拦截攻击数/攻击总数`，`false_positive_rate = 误拦良性数/良性总数`）

**落地细节**：三层防护各对应一个"拦截判定"——`injection_guard` 用 `InjectionGuard.check(input).flagged`，`ssrf_guard` 用 `SSRFGuard.check(input).allowed == False`，`redactor` 用 `Redactor.redact(input).count > 0`；脚本 `intercept()` 统一为 `bool`，`evaluate_cases()` 逐条对比守卫行为与数据集标签产出 `correct`，`summarize()` 聚合指标（空集不除零）。SSRF 用注入的 `fake_resolver`（`api.erp.example.com→93.184.216.34` 公网 / `partner.erp.example.com→10.0.0.5` 内网）模拟 DNS 重绑定攻击，整个评测无 LLM、无网络、无 DB，完全离线可复现。数据集输入逐条与守卫正则精确匹配（10 条注入/越权命中 IGNORE_PRIOR_INSTRUCTIONS/BYPASS_APPROVAL/LEAK_SYSTEM_PROMPT，5 条 SSRF 命中 BLOCKED_IP/BLOCKED_SCHEME/HOST_NOT_ALLOWED/BLOCKED_PORT，5 条秘密泄露命中 API_KEY/CN_MOBILE/CN_ID_CARD），含 1 条"忽略发货延迟"误报陷阱（含"忽略"但不属于"忽略…指令"家族，期望 ALLOW，测守卫的窄口径）。运行：`uv run python evals/scripts/run_security_eval.py`（可加 `--report` 输出 JSON）。实测 25/25 全对：攻击拦截率 100%、误报率 0%。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 评测先于优化 | 5.10 给 5.3/5.4/5.5 的三个守卫建了回归基线——"如果你不能测量它，你就不能改进它"。以后任何守卫改正则，先跑 25 条确认拦截率不掉 |
| 攻击拦截率 vs 误报率 | 拦截率 = 守卫抓住多少真攻击（召回）；误报率 = 误伤多少正常输入（精度）。单看拦截率会催生"全拦截"的懒守卫，必须两个指标一起看 |
| 确定性评测 | 无 LLM 是关键：守卫是纯函数，输入定了输出就定，测试才能断言"守卫行为 == 数据集标签"。SSRF 用注入 resolver 把 DNS 变成查表，消除网络不确定性 |
| 数据驱动标签 | `expected` 是人对用例的标注，`intercepted` 是守卫实际行为——测试断言两者相等，就是把"评测集"变成"可执行验收单" |
| 误报陷阱用例 | sec-022 "请忽略发货延迟的情况"含"忽略"但不触发注入正则，验证守卫口径窄、不会草木皆兵——这是生产里最容易被误伤的对话场景 |

---

# 阶段六：评测、可观测性与交付（预计 10-12 个任务）

> **阶段教学引言**
>
> "如果你不能测量它，你就不能改进它。"——这是工程界的铁律。
>
> Agent 系统比传统软件更难评估，因为它的输出是自然语言，正确性不是简单的 true/false。
> 我们需要建立一套科学的评测体系。
>
> 这个阶段你会学到：
> - **OpenTelemetry**：分布式追踪标准，一个 run_id 串联所有服务调用
> - **Prometheus**：时序数据库，用来采集和展示系统指标
> - **Eval Harness**：自动化评测框架，像考试一样批改 Agent 的"答卷"

---

## 任务 6.1：结构化日志

**目标**：所有日志为 JSON 格式，包含 run_id。

**交付物**：
- `src/erp_copilot/observability/logging.py`（✅ 已实现）
- `src/erp_copilot/observability/__init__.py`（✅ 已实现，包导出）
- `tests/unit/observability/test_logging.py`（✅ 已实现，16 用例）

**验收标准**：
- ✅ 每行日志是合法 JSON（`JsonFormatter.format` 产出一条 `json.dumps` 单行对象）
- ✅ 包含 timestamp、level、run_id、message、extra（另有 `service`/`logger`/`step_id`/`request_id`）
- ✅ run_id 自动从上下文获取（`ContextVar[TraceContext]` + `trace_context()` 上下文管理器）

**落地细节**：`TraceContext`（frozen dataclass：`run_id`/`step_id`/`request_id`）存在 `ContextVar` 中，`trace_context(run_id=..., step_id=..., request_id=...)` 上下文管理器用**合并语义**（内层只覆盖传入字段、保留外层——6.2 里「API 层绑 run_id → Step 执行器绑 step_id」可逐层叠加），block 结束 `reset` 恢复。`JsonFormatter` 是 stdlib `logging.Formatter` 子类（无第三方依赖，遵守"不添加未批准的依赖"），字段顺序稳定；`extra` = `record.__dict__` 减去 `_STDLIB_ATTRS` 白名单（即 `logger.info(..., extra={...})` 注入的非标准键，标准属性不重复）；`timestamp` 用 `record.created` 转 ISO8601+UTC（确定、带时区）；`json.dumps(default=str)` 兜底非可序列化值。`setup_logging(level, service)` 幂等配置 root logger（清旧 handler 换 JSON StreamHandler），`Settings.log_level`/`app_name` 的对接留到入口（apps/api）接线任务。调用：任意组件 `logging.getLogger(...)` 即可，run_id 自动带上——满足 docs/08 §6"禁止无法关联 Run 的自由文本日志"。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 结构化日志 vs 自由文本 | 一行日志是一个 JSON 对象，聚合器（Loki/CloudWatch/ELK）按字段索引，无需正则解析文本；docs/08 §6 规定字段契约（timestamp/level/service/run_id/step_id/...），这是可观测性的地基 |
| ContextVar = 隐式调用上下文 | `ContextVar` 是每个线程/协程独立的一份"全局变量"。run_id 在 API 入口绑一次，整个调用树里的 logger 都能读到——避免了把 run_id 当参数层层传的样板代码 |
| 合并语义的上下文管理器 | `trace_context` 只覆盖传入字段、保留外层（run→step 逐层叠加），用 `token = var.set(...)` / `var.reset(token)` 保证异常时也恢复 |
| 为什么自己写 Formatter | 项目规则"不添加未批准的依赖"，而 python-json-logger/structlog 只是把 stdlib 的 `Formatter` 包了一层——`logging.Formatter` 子类 + `json.dumps` 就能实现同样的效果，零新增依赖 |
| setup_logging 幂等 | 清掉旧 root handler 再装 JSON handler，避免热重载/重复初始化时日志叠加成两行 |

---

## 任务 6.2：OpenTelemetry 集成

**目标**：分布式追踪：一个 run_id 串起所有服务。

**交付物**：
- `src/erp_copilot/observability/tracing.py`（✅ 已实现）
- `src/erp_copilot/observability/__init__.py`（✅ 追加导出）
- `tests/unit/observability/test_tracing.py`（✅ 已实现，9 用例）

**验收标准**：
- ✅ API→Worker→MCP Gateway→Simulator 在同一 Trace 中（W3C `traceparent` 传播助手 `inject_trace_headers` / `extract_trace_context`，下游 `span(context=...)` 续接同一 trace；实际跨服务接线在 apps 层后续任务）
- ✅ 每个 Span 有 run_id 属性（`span()` 自动从 6.1 TraceContext 读取 `run_id`/`step_id` 设为 Span 属性）
- ✅ LangGraph 每个节点是一个 Span（`node_span(name)` 装饰器，graph.py 接线时逐节点套用）

**落地细节**：`setup_tracing(service_name, exporter)` 建**模块级** `TracerProvider`（`Resource(service.name)` + `SimpleSpanProcessor` 同步导出、无后台线程；默认 ConsoleSpanExporter，测试注入 `InMemorySpanExporter`）。关键取舍：不调全局 `trace.set_tracer_provider`——OTel SDK 拒绝重复覆盖全局 provider（"Overriding of current TracerProvider is not allowed"），模块级让各进程 instrumentation 独立、测试免跨测试状态。`span(name, attributes, context)` 是 `start_as_current_span` 包装：自动取 run_id/step_id（未绑定时省略——OTel 拒绝 None 属性）、异常时 `record_exception` + `Status.ERROR` 再抛出；`get_tracer()` 懒初始化（未配置时自动装默认 provider）。`node_span` 用 `@wraps` 保留签名，Span 名与 `erp.node` 属性都带节点名。传播测试验证：upstream Span 内 inject 出 `traceparent`，下游 extract 后开 Span，与 upstream 同 `trace_id` 且 parent=upstream（跨进程边界续接同一 Trace）。Span 不携带密钥（docs/08 §5"不得记录完整密钥"）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Trace 是什么 | 一次完整请求的全链路记录：API→Agent→MCP→ERP，所有步骤一条 Trace；用 W3C traceparent 跨进程传播（上游 inject 请求头、下游 extract 续接），跑题"一个 run_id 串起所有服务" |
| Span 是什么 | Trace 中的一段：一次 LLM 调用、一次工具调用都是一个 Span，父子关系组成树——`span()` 嵌套即父子 |
| run_id 传递 | 6.1 的 TraceContext（ContextVar）与 Span 属性打通：`span()` 自动把 run_id 挂到每个 Span，Grafana 中按 Run 过滤 |
| 模块级 vs 全局 provider | OTel SDK 全局 provider 只准设一次；模块级 provider 让本模块显式 span 可控、可注入 exporter，测试用 `InMemorySpanExporter` 完全离线断言 |
| 懒初始化 | `get_tracer()` 未配置时自动装默认 provider，业务代码无需先调 setup 就能打 span；生产在入口用 OTLP exporter 替换 |

---

## 任务 6.3：Langfuse 集成

**目标**：LLM 专用的可观测性（Prompt、Token、延迟、成本）。

**交付物**：
- `src/erp_copilot/observability/langfuse.py`（✅ 已实现）
- `src/erp_copilot/observability/__init__.py`（✅ 追加导出）
- `tests/unit/observability/test_langfuse.py`（✅ 已实现，10 用例）

**验收标准**：
- ✅ 每次 LLM 调用记录输入/输出 Token 数（`call.input_tokens` / `output_tokens` 填充后写入 Span 属性与 `LLM_CALL` 日志，`total_tokens` 由两者求和）
- ✅ 每次调用记录延迟（`time.perf_counter()` 起止，`latency_ms` 毫秒级；异常路径同样记录）
- ✅ 可按 Run 查看 LLM 调用链（`llm_call()` 内开 `llm.{model}` Span，`span()` 自动携带 6.1 的 run_id，一次 Run 内多次 LLM 调用共享 run_id，按 Run 过滤即得调用链）

**落地细节**：项目规则禁止未批准依赖，故不引入官方 `langfuse` SDK，而是用 6.1/6.2 自建栈实现 Langfuse 风格 LLM 可观测性：`llm_call(model, prompt, system_prompt)` 是 `@contextmanager`，内部先 `span(f"llm.{model}")` 开 Span，`yield` 后由调用方回填 `completion`/`input_tokens`/`output_tokens`；退出时 `_finalize()` 把 Token、延迟、成本写入 Span 属性，并 `logger.info` 一条 `LLM_CALL` 结构化日志（6.1 JsonFormatter 自动带 run_id）。异常路径在 `except` 里补 `error` 属性、Span 标记 ERROR 后重抛（`_finalize` 在 `with span` 块内执行，保证异常时属性在 Span 关闭前落盘）。成本由 `estimate_cost()` 纯函数查 `_PRICING_USD_PER_1K` 价格表（gpt-4o / gpt-4o-mini / deepseek-chat，标注"示意价格"），未知模型或缺少 Token 返回 None、Span 省略成本属性。模块 docstring 明确：未来批准 SDK 后替换 `llm_call` 函数体即为干净换点。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| LLM 可观测性为什么特殊 | 不只是调用成功与否：Prompt 大小、Token 数、延迟、成本是评估模型性价比与调优的基础；Langfuse 是这一品类的代表产品 |
| 为什么不用官方 SDK | 项目规则"不添加未批准依赖"；用自建栈 + 干净换点（docstring 标注替换 `llm_call` 函数体）既守规则又保留可迁移性 |
| contextmanager 承担"测量器" | `llm_call` 只测量包住它的代码块：开始计时、结束算延迟、异常记 error——调用方只需在块内回填 Token，职责清晰 |
| 成本估算 | `estimate_cost` 纯函数查价格表（$/1K tokens × 输入/输出分别计价）；价格表标注"示意"，需按供应商最新价维护 |
| 与 span/log 的关系 | 一次 LLM 调用 = 一个 `llm.{model}` Span（run_id 可过滤调用链）+ 一条 `LLM_CALL` JSON 日志，两路都服务于"按 Run 复现一次对话" |

---

## 任务 6.4：Prometheus 指标

**目标**：暴露系统指标供 Grafana 展示。

**交付物**：
- `src/erp_copilot/observability/metrics.py`（✅ 已实现）
- `apps/api/routes/metrics.py`（✅ 已实现，`GET /metrics` 端点）
- `apps/api/main.py`（✅ include metrics 路由）
- `tests/unit/observability/test_metrics.py`（✅ 已实现，9 用例）
- `tests/unit/api/test_metrics_routes.py`（✅ 已实现，4 用例）

**验收标准**：
- ✅ `GET /metrics` 返回 Prometheus 格式指标（`Response(content=generate_latest(), headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})`，body 为 `# HELP`/`# TYPE`/sample 三部分组成的文本直方图格式）
- ✅ 包含：Run 创建数、完成数、失败数（三个 Counter：`erp_runs_created_total` / `_completed_total` / `_failed_total`）、各阶段延迟 P50/P95（Histogram `erp_phase_latency_seconds` 带 `phase` 标签，桶 0.1s~300s，P50/P95 由 Grafana PromQL `histogram_quantile(0.5/0.95, rate(...))` 服务端计算）
- ✅ Worker 队列长度（Gauge `erp_worker_queue_length`，`set_worker_queue_length(n)` 供 worker 层接线）

**落地细节**：依赖经用户批准新增 `prometheus-client==0.26.0`（uv.lock 锁定）。`metrics.py` 用工厂 `create_metrics()` 返回 frozen dataclass `Metrics`（`registry` + 3 Counter + 1 Histogram + 1 Gauge），每次调用建独立 `CollectorRegistry`——prometheus_client 拒绝同注册表重复注册同名指标，工厂模式让测试完全隔离（测试各自建新实例，绝无跨测试状态，呼应 6.1/6.2 的测试纪律）。生产走模块级单例 `METRICS`，`generate_latest(metrics=METRICS)` 序列化为文本。P50/P95 不在进程内算：直方图只输出 `_bucket/_sum/_count`，Grafana 用 `histogram_quantile` 计算——这是 Prometheus 标准做法，避免客户端与服务器两端维护分位数状态。桶值从默认 0.005s~10s 调到 0.1s~300s，覆盖 agent 阶段（LLM 调用链可达数分钟），否则 P95 在 >10s 段失真。计数器/Gauge 是进程内状态：多 worker 进程各自独立，Grafana 按 `sum()` 聚合（指标名已带 `_total` 便于 rate）。本任务只做采集与暴露；run 生命周期在各层接线（API/worker 的 inc、observe）留给后续任务，避免一次性改动过大。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Prometheus 拉取模型 | Prometheus 主动 `GET /metrics` 拉取文本指标，而非应用主动推送——所以端点必须返回纯文本格式，且内容类型固定 `text/plain; version=0.0.4` |
| Counter / Gauge / Histogram | Counter 只增不减（适合"创建数"），Gauge 可增可减反映当前值（适合"队列长度"），Histogram 记录观测分布（延迟），三种语义对应三种监控问题 |
| 为什么 P50/P95 用 PromQL 算 | 直方图桶是增量的、跨进程可合并；分位数若在客户端算，多进程无法聚合且浪费内存。`histogram_quantile(0.5, rate(m_seconds_bucket[5m]))` 是标准公式 |
| 文本格式三件套 | `# HELP`（指标含义，Grafana 展示用）+ `# TYPE`（指标类型，Prometheus 校验）+ sample（值）；格式错误会让 Prometheus 抓取失败 |
| 桶设计 | 桶覆盖预期值域且在 P50/P95 附近加密；`+Inf` 桶必须存在（总计）。默认桶到 10s 对 agent 场景不够，故扩展至 300s |
| 进程内状态 | Counter/Gauge 存在进程内存里，多 worker 需 `sum()` 聚合；这也是监控要考虑多实例时的标准坑 |

---

## 任务 6.5：扩展评测数据集——200 条

**目标**：按 V6 设计文档完成完整评测集。

**交付物**：
- `evals/datasets/tool_retrieval_40.json`（✅ 40 条，tool-001..040）
- `evals/datasets/knowledge_rag_40.json`（✅ 40 条，rag-001..040）
- `evals/datasets/planning_50.json`（✅ 50 条，plan-001..050）
- `evals/datasets/recovery_25.json`（✅ 25 条，rec-001..025）
- `evals/datasets/failure_20.json`（✅ 20 条，fail-001..020）
- `evals/datasets/security_25.json`（✅ 25 条，复用 5.10 已提交文件，不计新改动）
- `tests/unit/evals/test_datasets_200.py`（✅ 已实现，24 用例）

**验收标准**：
- ✅ 6 类共 200 条，每条可独立执行（40+40+50+25+25+20=200，case_id 文件内唯一）
- ✅ 期望结果可量化比（expected_tool / should_answer / steps / expected_action / expected_behavior / expected_duplicate_writes / expected 全部机器可断言）
- ✅ 每条用例锚定权威来源：9 个 V6 工具名、`(domain, action)` 映射、23 个知识库 document_id、模拟器种子数据（6 商品 / 5 供应商 / 10 区域 / 库存量）

**落地细节**：全部 200 条由 `tests/unit/evals/test_datasets_200.py` 做结构校验与领域锚定（无 LLM、无网络）。分布：Tool Retrieval 40 条覆盖 9 工具、11 条 hard_negative、查询意图（query/check_stock）绝不期望写工具；Knowledge RAG 40 条沿用 `datasets/eval/retrieval_queries.yaml` v2 人工标注映射（34 可答 + 6 无答案拒答，无答案 relevant_docs 为空）；Planning 50 条 = 20 单步 + 30 多步，多步用 `$N.param` 编码计划 DAG（校验器断言引用只指向先前步骤、首步无前向依赖），createOrder 数量全部 ≤ 对应库存；Recovery 25 条覆盖 ask_missing(12)/confirm_conflict(5)/retry(4)/reject(4)，动作与缺参/冲突字段自洽（ask_missing 必有 missing_params 且 conflict 为 null）；Failure 20 条覆盖 7 类场景（worker_crash/tool_timeout/tool_5xx/tool_429/user_cancel/deadline/reconciliation），worker_crash 与 reconciliation 断言 expected_duplicate_writes=0。数据集 drift（改了种子数据或工具名）会被测试在 CI 拦下。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 为什么"可量化"先于"准确" | 评测集先要每个期望结果能机器断言（工具名、布尔、枚举、数量），LLM 评分器（6.6 harness）才站得住；否则"答得好不好"无法复现 |
| 人工标注映射 vs 生成式造数据 | RAG 的 relevant_docs 沿用 retrieval_queries.yaml 的 v2 人工标注，可答/拒答分开；生成式造数据容易自我循环（LLM 造题又 LLM 评） |
| 用 `$N.param` 编码计划 DAG | 多步规划用例用 `$1.product_id` 引用前一步输出，校验器反推依赖合法性（只能向后引用），数据集即"可执行计划断言" |
| 领域锚定 = 数据集防漂移 | 用例里的商品名/区域/库存量全部指向种子数据常量，种子一改测试即红，避免"评测跑过但域名不存在"的假绿 |
| 无答案拒答单独成类 | should_answer=false 的 6 条用于无答案拒答率指标；相关文档为空是硬性约定，防止拒答用例偷偷配了文档 |
| 一个测试文件校验全部六类 | 24 个用例统一 schema、枚举、种子锚定与计数；后续增删用例改计数，测试强制同步 |

---

## 任务 6.6：Eval Harness——自动化评测框架

**目标**：一键运行所有评测并输出报告。

**交付物**：
- `evals/harness.py`（✅ runner 无关编排框架：加载数据集、注入 runner、聚合得分、失败日志、JSON 报告）
- `evals/run_all.py`（✅ 6 个类别 runner + CLI 入口）
- `tests/unit/evals/test_harness.py`（✅ 已实现，12 用例，假 runner 隔离框架）
- `tests/unit/evals/test_run_all.py`（✅ 已实现，7 用例，真实 runner 跑真实 200 条）

**验收标准**：
- ✅ `uv run evals/run_all.py` 运行全部 200 条（6 类 40+40+50+25+25+20，无 LLM/DB/网络，全部离线可复现）
- ✅ 输出每类得分和总体得分（`primary_score` 按用例数加权成总体分；每类带 `mode` 标注来源）
- ✅ 失败案例有详细日志（`failures` 含 category/case_id/expected/actual/detail；文本报告逐条列出）

**落地细节**：harness 与 runner 解耦——runner 只返回 `{mode, primary_score, metrics, per_case}`，`run_category` 补齐 category/num_cases，`run_all` 按 `DATASET_SPECS` 顺序加载 6 个数据集并逐类 try/except 隔离（单类崩溃记入 `errors` 且该类计 0 分，绝不中断整轮）。run_all.py 的 6 个 runner 诚实标注 provenance：`tool_retrieval` 用 `filter_candidates` 的第一级 DOMAIN_TOOL_MAP 过滤器测"期望工具是否被召回"（40 条全部命中=1.0，锁定"9 工具规模下确定性分类学足够"这一不变量）；`security` 复用 5.10 的真实守卫（伪 DNS，拦截率 1.0/误报 0）；`knowledge_rag`/`planning`/`recovery`/`failure` 因 agent 运行时尚未接线，用 golden baseline（oracle 返回数据集期望答案）跑通管道，`mode` 字段明确标注，黄金 100% 不会被误当真实模型分；后续任务接真实 pipeline 时只换 runner 不动 harness。`score_retrieved` 复用 retrieval_scorer 算 recall@5/ndcg@5。报告 JSON 落盘 `evals/reports/`，`ensure_ascii=False` 保留中文。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 编排层与执行层解耦 | harness 只做"加载→注入→聚合→报告"，不知道任何类别的评分细节；runner 是唯一知晓数据集 schema 的地方，替换 runner 即换评测对象，框架零改动 |
| 失败隔离而非整体崩溃 | 单类 runner 抛异常记入 `errors`、该类按 0 分计入总体——评测跑完比"跑一半就断"更有诊断价值；同 Go 的 panic/recover 哲学，异于 Python 默认的快速失败 |
| 为什么要有 mode 标注 | docs/08 §1 要求指标可复现、不虚报；黄金 baseline 的 100% 与真实模型 100% 天差地别，provenance 字段让报告读者一眼分辨"测的是管道还是模型" |
| 总体分按用例数加权 | 200 条里各类数量不同（40/40/50/25/25/20），按用例数加权让大类别主导总体分，避免小类别的满分虚高总体 |
| golden baseline 的价值边界 | 它验证的是"harness 从加载到报告整条链路正确"，不是系统能力；这是 6.9 故障注入前唯一能端到端跑通全 200 条的骨架 |
| 确定性 runner 才是真指标 | tool_retrieval 的 recall@filter=1.0 是真实逻辑（DOMAIN_TOOL_MAP 覆盖全部期望工具）的不变量，测试锁死它，数据集或过滤器漂移即红 |

---

## 任务 6.7：Record/Replay 模式

**目标**：录制真实 LLM 响应，CI 中重放（不调付费模型）。

**交付物**：
- `evals/record_replay.py`（✅ RecordReplay 三模式 + JSONL 录制文件格式）
- `tests/unit/evals/test_record_replay.py`（✅ 已实现，13 用例，假 LLM 无网络）

**验收标准**：
- ✅ 录制模式保存 LLM 响应（`record` 模式包装真实 callable，`save()` 写 JSONL）
- ✅ 重放模式用录制结果替代真实调用（`replay` 模式按 prompt 精确匹配，绝不触碰 provider）
- ✅ CI 中不调用付费模型也能跑评测（`wrap()` 即 LLM 注入 seam，可直接传给 `build_plan_node(llm_complete=...)`）

**落地细节**：无任何 src 改动——包装点就是 agent 节点注入的 `llm_complete: Callable[[str], str]` seam。`load_recording` 读 JSONL（每行 `{"prompt","completion"}`，`setdefault` 保证重复 prompt 先到先得）；`RecordReplay.__init__` 校验 mode（非法即 ValueError），replay 模式构造期就加载录制文件（文件缺失抛 FileNotFoundError，CI 里 stale 录制立即暴露）。`wrap()` 三态：off 原样返回原 callable（`is` 恒等）；record 包一层先调真 provider 再入内存 buffer，`save()` 将 buffer 与已有文件合并（已有条目优先=先到先得）后整体重写，幂等可重复调；replay 服务已录完成，未命中抛 `ReplayMissError(KeyError)`（严格重放，陈旧录制在 CI 中大声失败而非静默降级）。测试覆盖 record→replay 端到端（用调用计数器证明重放时 provider 零调用）、重复 prompt 先到先得、以及 wrapped callable 真实插入 `build_plan_node` 的 seam 冒烟（录制了含中文 query 的完整 planner prompt）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 录制重放（Record/Replay） | 把"昂贵的真实调用"与"可复现的测试"解耦：首次在真环境录一次，之后离线重放完全一致的结果；比 mock 强在"录的是真实分布"，比每次都调真模型强在"免费、快、确定" |
| 接缝（seam）注入而非改造 | 不改 src：agent 节点本来就接受 `llm_complete: Callable[[str], str]`，包装这个 seam 就同时覆盖了所有 agent 节点，测试里 `build_plan_node(llm_complete=recorder.wrap(...))` 证明它真能插进去 |
| JSONL 追加友好格式 | 每行一个 JSON 对象、无顶层包裹，可增量追加、可 tail/awk 检查；相比单一大 JSON 文件更适合"录一次、跨 CI 重放"的场景 |
| 先到先得 vs 覆盖 | 重复 prompt 取第一条：重放确定性优先——先录的才是"真"，后录的可能是抖动，`setdefault` 一行实现 |
| 严格重放（fail loud） | 未命中 prompt 抛 `ReplayMissError` 而非回退真调用：CI 里录制陈旧/数据漂移立即红，绝不静默降级成"这次没测到" |
| 与 mock/免费回退的区别 | `off` 模式即原样透传（不付任何代价）；这是三档开关——录（真调用+记账）、放（纯查表）、关（裸用），一个 seam 三个环境（本地/CI/生产）复用 |

---

## 任务 6.8：负载测试——Locust

**目标**：压测系统并发能力。

**交付物**：
- `tests/performance/locustfile.py`（✅ Locust 入口，50 并发用户提交 Run）
- `tests/performance/load_scenario.py`（✅ 纯场景逻辑：路径/载荷/请求处理，可离线单测）
- `tests/unit/performance/test_locustfile.py`（✅ 已实现，11 用例，假 client 无网络）

**验收标准**：
- ✅ 模拟 50 并发用户提交 Run（`uv run locust -f tests/performance/locustfile.py --headless -u 50 -r 5 -t 60s --html ...`，`-u` 可切 1/10/25 并发）
- ✅ 记录 P50/P95 延迟、吞吐量、错误率（Locust 统计页/`--html` 报告自带；非 202 记失败入错误率）
- ✅ 输出 HTML 报告（`--html tests/performance/reports/locust_report.html`）

**落地细节**：压测目标 = `POST /v1/runs`（系统入口：DB INSERT + Celery 入队，正对 docs/08 §4 "API enqueue P50/P95"）。`load_scenario.py` 把可测核心与 Locust 解耦——**Locust 2.46 的 `__init__.py` 在 import 时调用 `gevent.monkey.patch_all()`，在 pytest 进程里（ssl 已被 urllib3/anyio 加载）重打补丁会 `RecursionError`，所以单测绝不 import locust**；场景逻辑（`RUN_PATH`、`build_run_payload`、`submit_run(client)`，client 用 `Protocol` 鸭子类型化）放在无 Locust 依赖的模块里，`locustfile.py` 只是薄包装 `submit_run(self.client)`。`runs.tenant_id` 是外键，压测前需种租户行（确定性 id `loadtest-tenant`，幂等 SQL，见 load_scenario docstring）；提交路径不调 LLM，天然满足 docs/08 §9 "固定 Mock LLM" 要求。租户 id 可用 `LOAD_TEST_TENANT_ID` 环境变量覆盖。真实压测需本地栈（Postgres+Redis+uvicorn），单测用假 client 确定性验证载荷/路径/非 202 记失败；locustfile 的 task 接线用独立解释器冒烟验证（pytest 内无法 import）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Locust 原理 | 每个虚拟用户是一个 gevent 协程，循环执行 `@task`，`wait_time` 控制思考间隔；`-u` 并发数、`-r` 起步速率、`-t` 时长；`--html` 出报告，P50/P95/吞吐/错误率都来自每个请求的计时与成败 |
| 为什么测 enqueue 而非全流程 | `POST /v1/runs` 一个请求 = DB 写 + Redis 入队，是系统吞吐的咽喉；全流程（agent 执行）归任务 6.9 故障注入与端到端测，职责分开 |
| catch_response 的错误率语义 | `catch_response=True` 下非 202 调 `resp.failure()`，请求仍完成但计入错误率——比抛异常让任务中断更真实（用户收到了响应只是业务失败） |
| 为什么场景逻辑不能放 locustfile | Locust 在 import 时全局 monkey-patch（gevent patch ssl），pytest 进程里再打补丁直接 RecursionError——重依赖入口与可测核心分离，入口只留"薄胶水"，测试打纯逻辑 |
| 外键是压测的先决条件 | `runs.tenant_id → tenants.id`，不存在租户直接 500 刷爆错误率；用确定性 id + `ON CONFLICT DO NOTHING` 幂等种入，压测可重复 |
| Protocol 鸭子类型代替真实 HttpSession | 测试不需要真实 Locust client，用带 `post` 方法的假对象满足 `Protocol` 即过——只测"我们要的那部分接口" |

---

## 任务 6.9：故障注入和恢复测试

**目标**：验证系统在异常情况下的行为。

**交付物**：
- `tests/performance/fault_injection.py`（✅ 故障注入与恢复场景：worker_recovery / erp_timeout_retry / order_idempotency）
- `tests/unit/performance/test_fault_injection.py`（✅ 已实现，8 用例，含 3 个"故障未注入必须报 FAIL"的负例）

**验收标准**：
- ✅ Worker 被 kill→任务恢复（`inject_worker_crash` 落 checkpoint + 幂等记录，恢复仅对 PENDING 写步骤标 `RECOVERY_RECONCILIATION_REQUIRED`，COMPLETED 永不重跑）
- ✅ ERP 超时→正确重试或失败（504 标可重试 → 恢复后 RetryExecutor 重试到 201，超时对库存零影响）
- ✅ 订单创建不重复（同幂等键 POST 两次 → 同一 order_id，库存只扣一次）

**落地细节**：worker 任务（apps/worker/tasks.py）从不真正调用 agent 图，所以"kill worker 再恢复"无法端到端演示——故障在组件边界建模（新 SQLite 引擎 = 新进程，复刻 test_recovery.py 的跨会话可见性模型）：`inject_worker_crash` 写入 EXECUTING 的 AgentState checkpoint（s1 PENDING / s2 COMPLETED 两个写步骤），`RunRecovery.load` 后 s1 必须被标对账、s2 必须被跳过。ERP 超时场景：caller 先 `PUT /scenario/timeout` 注入故障 → 探测 POST 得 504 且库存不变（超时在落库前抛错）→ 恢复 happy_path → `RetryExecutor(sleep=noop)` 重试到 201 → 同键再 POST 得 200 同 order_id。订单幂等场景：同键两次 → 201/200 同 order_id + 库存只扣一次。三个负例证明 harness 非"真空绿"：无 checkpoint 的引擎、无 timeout 的 client 都会让对应场景报 passed=False。模拟器场景/库存是进程级全局，故每个场景用 uuid 唯一键 + 相对库存差断言；无新增依赖，零 src 改动，可独立运行 `uv run python tests/performance/fault_injection.py`（3/3 PASS）。

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| 组件边界建模代替真实进程 kill | 真实 worker 不重放 agent 图，端到端杀进程无法演示恢复；用"新 Session = 新进程"共享同一 SQLite 文件，恢复逻辑对 checkpoint+幂等记录的判读完全一致——测的是恢复算法本身，不是进程编排 |
| 对账判定 PENDING vs COMPLETED | PENDING 幂等记录 = 意图已写、结果未知（可能已生效）→ 必须标 RECOVERY_RECONCILIATION_REQUIRED 让人工确认；COMPLETED 记录/步骤 = 已证明确凿 → 永不重跑，防重复副作用 |
| 可重试的判定 | 504 是"已证明未执行"的瞬时失败（超时在落库前抛错，探测后库存不变佐证），标 is_retryable=True；非幂等的业务 4xx 不可重试——`RetryPolicy.is_retryable_http` 只放 429 和 5xx |
| RetryExecutor 的可注入 sleep | `sleep=lambda _: None` 使退避零等待，单测/harness 确定性驱动重试序列，无需真等 1s/2s/4s |
| 幂等键的唯一性保证 | 客户端每次用 uuid 新键，模拟器按 (tenant_id, idempotency_key) 去重——重试与重复提交都收敛到同一 order_id，库存只扣一次 |
| 负例防"真空绿" | 每个场景都配"故障未注入"的负例：harness 必须能检测到故障缺失并报 FAIL，否则断言永远过，测了等于没测 |

---

## 任务 6.10：完成文档和演示 ✅

**目标**：补全项目文档和演示视频。

**交付物**：
- `docs/product.md`、`docs/architecture.md`、`docs/api-contracts.md`
- `docs/evaluation.md`、`docs/threat-model.md`、`docs/benchmark.md`
- `README.md`（含架构图、一键启动说明）

**完成情况**：
- 7 个交付文件全部创建（`docs/product.md`、`docs/architecture.md`、`docs/api-contracts.md`、`docs/evaluation.md`、`docs/threat-model.md`、`docs/benchmark.md`、根 `README.md`），已提交。
- 文档诚实标注三处未接线点：`/v1/knowledge/search` 为 STUB；Celery worker `execute_run` 为简化直连路径、未接入完整 LangGraph 状态机；`evals/run_all.py` 六分类中 `tool_retrieval`/`security` 跑真实逻辑、其余四个为 `golden_baseline`。
- `docs/benchmark.md` 只写真实测量值：42 条查询消融（Vector-only / Hybrid / Hybrid+Rerank 的 Recall@1/5、MRR、NDCG@5、P@5、P50/P95）、故障注入 3/3 PASS、单元测试 874 通过、mypy 55 文件干净；负载测试明确标"待实测"。

**验收标准**：
- [x] 六份交付文档 + 根 README 齐全
- [x] README 含 mermaid 架构图与一键启动命令（docker compose + 四个 uvicorn/celery 进程）
- [x] 基准数字全部可复现（`docs/benchmark.md` §6 给出复现命令）
- [x] 未测量项（负载 P50/P95、安全拦截率）标注"待实测/待列出"，不编造数字

---

## 任务 6.11：清理和 Release v1.0 ✅

**目标**：清理敏感信息，打 Tag。

**交付物**：
- 无密钥、无日志、无大文件
- Git tag v1.0.0

**完成情况**：
- [x] 移除冻结的 V5 遗留目录 `agent-copilot-v5-20260317/`（51 个跟踪文件 + 294MB 磁盘，含 17MB/18MB 日志），提交 `9d9ff87`。
- [x] 加固 `.gitignore`（`.env.*.local`、`logs/`、`*.log`、mypy/pytest/ruff/code-review-graph 缓存），杜绝日志与缓存入库。
- [x] 补交全部缺失核心源码（apps / src / migrations / infra / datasets / tests / session-history，127 个文件 + 8 个修改文档），提交 `6f534de`，保证 v1.0.0 标签可用。
- [x] 门禁全绿：`ruff check`/`format` 通过、`mypy src` 55 文件干净、`pytest tests/unit` 874 通过（提交 `0954377` 修复引导文件的 10 处 ruff 问题并把 V5 数据集迁移到 `tests/unit/tools/fixtures/`）。
- [x] 提交树无密钥/无日志/无大文件（最大文件 `uv.lock` 901KB）。
- [x] 创建注解标签 `v1.0.0`，指向 `0954377`。

---

## 任务 6.12：[REDACTED]表述和讲稿 ✅

**目标**：准备[REDACTED]材料。

**交付物**：
- 项目描述（含实测数字）
- 10 分钟讲解大纲
- 常见 Q&A

**完成情况**：
- [x] 新建 `docs/12-resume-and-pitch.md`：§1 项目描述（一段式 200 字 + 两行式 + 数字速查表 + 诚实边界）、§2 十分钟讲解大纲（逐分钟脚本）、§3 高频 Q&A（选型 / Agent / 安全 / 一致性 / 评测 / 反思）。
- [x] 所有数字实测可复现：874 单测、mypy 55 文件、42 条检索消融（Rerank Recall@5 0.7381→0.9167）、故障注入 3/3、安全评测 25 条拦截率 100%/误报 0%（本次实跑 `run_security_eval.py`）；负载测试诚实标"待实测"。
- [x] 主动交代三条诚实边界（knowledge search STUB、worker 简化直连、4/6 golden_baseline）。
- [x] `docs/README.md` 文档导航补 `12-resume-and-pitch.md` 条目。

---

# 任务统计

| 阶段 | 任务数 | 预计天数（每天 3-4 小时） |
|------|--------|--------------------------|
| 一：工程骨架 | 12 | D1-D5 |
| 二：工具与模拟器 | 12 | D6-D10 |
| 三：知识检索 | 14 | D11-D15 |
| 四：Agent Runtime | 16 | D16-D22 |
| 五：安全审批恢复 | 10 | D23-D26 |
| 六：评测与交付 | 12 | D27-D30 |
| **合计** | **76** | **30 天** |

---

# 如何使用本计划

1. **每个任务开始前**：阅读「教学要点」列，理解技术原理。
2. **任务执行**：严格按 TDD 三步骤（分析→测试→实现→审查）。
3. **任务完成后**：确认验收标准全部满足，再进入下一个任务。
4. **遇到不理解的技术点**：停下来提问，不要跳过。
