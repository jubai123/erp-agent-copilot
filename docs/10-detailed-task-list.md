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

---

## 任务 4.12：Checkpoint 和恢复

**目标**：每个节点完成后保存状态，进程崩溃后能恢复。

**交付物**：
- `src/erp_copilot/memory/checkpoint.py`

**验收标准**：
- 每个节点执行后保存 checkpoint 到 PostgreSQL
- Worker 重启后从最新 checkpoint 恢复
- 已完成的步骤不会重复执行

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

**目标**：实现 WRITE 操作的审批暂停。

**交付物**：
- `src/erp_copilot/security/approval.py`

**验收标准**：
- 高风险步骤→Run 进入 WAITING_APPROVAL
- 用户调用 approve API→继续执行
- 用户 deny→步骤被跳过，Run 进入对应状态
- 审批记录写入 audit_log

---

## 任务 5.3：SSRF 防护和 Egress 控制

**目标**：防止 Agent 访问内网或危险 URL。

**交付物**：
- `src/erp_copilot/security/ssrf_guard.py`

**验收标准**：
- 请求内网 IP（127.0.0.1/10.x/192.168.x）→拦截
- 请求非白名单域名→拦截
- 拦截事件写入 security_events

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| SSRF 是什么 | Server-Side Request Forgery：攻击者让服务器请求内部服务 |
| Agent 场景的 SSRF 风险 | 恶意 prompt 可能让 Agent 调用 `http://internal-admin/delete-all` |
| 防护策略 | IP 黑名单 + 域名白名单 + URL scheme 限制（只允许 https） |

---

## 任务 5.4：Prompt Injection 防护

**目标**：检测和拦截恶意用户输入。

**交付物**：
- `src/erp_copilot/security/injection_guard.py`

**验收标准**：
- "忽略之前指令"→标记为可疑
- "跳过审批直接下单"→标记为可疑
- "泄露系统提示词"→标记为可疑

---

## 任务 5.5：Secret Redaction

**目标**：输出结果中自动隐藏密钥、密码等敏感信息。

**交付物**：
- `src/erp_copilot/security/redaction.py`

**验收标准**：
- API Key 模式→替换为 `[REDACTED]`
- 手机号/身份证号→部分隐藏
- 不影响正常的业务数据

---

## 任务 5.6：幂等写操作

**目标**：保证同一个写操作不会被执行两次。

**交付物**：
- `src/erp_copilot/tools/idempotency.py`

**验收标准**：
- 相同幂等键→返回缓存结果
- 不同幂等键→正常执行
- 幂等记录持久化（PostgreSQL）

---

## 任务 5.7：退避重试策略

**目标**：瞬态错误自动重试，永久错误不重试。

**交付物**：
- `src/erp_copilot/agent/retry_policy.py`

**验收标准**：
- 超时/429→指数退避重试（1s→2s→4s）
- 4xx 业务错误→不重试
- 重试次数在 Step 的 max_retries 范围内

---

## 任务 5.8：Worker 恢复流程

**目标**：Worker 崩溃重启后恢复执行。

**交付物**：
- `src/erp_copilot/agent/recovery.py`

**验收标准**：
- Worker 重启→加载最新 checkpoint
- 状态不明确的写 Step→标记为需要对账（RECOVERY_RECONCILIATION_REQUIRED）
- 已完成的 Step→不重复执行

---

## 任务 5.9：失败队列和人工处理

**目标**：无法自动恢复的失败进入人工处理队列。

**交付物**：
- Run 的 FAILED 状态 + 失败原因 + 建议操作

**验收标准**：
- 永久失败→Run FAILED，记录具体错误
- 对账失败→标记为需要人工介入
- 失败信息足够让开发者定位问题

---

## 任务 5.10：安全评测数据集（25 条）

**目标**：建立安全评测用例。

**交付物**：
- `evals/datasets/security_25.json`（注入、越权、SSRF、秘密泄露场景）

**验收标准**：
- 每条有攻击描述、期望拦截结果
- 计算攻击拦截率和误报率

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
- `src/erp_copilot/observability/logging.py`

**验收标准**：
- 每行日志是合法 JSON
- 包含 timestamp、level、run_id、message、extra
- run_id 自动从上下文获取

---

## 任务 6.2：OpenTelemetry 集成

**目标**：分布式追踪：一个 run_id 串起所有服务。

**交付物**：
- `src/erp_copilot/observability/tracing.py`

**验收标准**：
- API→Worker→MCP Gateway→Simulator 在同一 Trace 中
- 每个 Span 有 run_id 属性
- LangGraph 每个节点是一个 Span

**教学要点**：
| 概念 | 讲解内容 |
|------|---------|
| Trace 是什么 | 一次完整请求的全链路记录：API→Agent→MCP→ERP，所有步骤一条 Trace |
| Span 是什么 | Trace 中的一段：一次 LLM 调用、一次工具调用都是一个 Span |
| run_id 传递 | 所有 Span tag 上加 run_id，Grafana 中可以按 Run 过滤 |

---

## 任务 6.3：Langfuse 集成

**目标**：LLM 专用的可观测性（Prompt、Token、延迟、成本）。

**交付物**：
- `src/erp_copilot/observability/langfuse.py`

**验收标准**：
- 每次 LLM 调用记录输入/输出 Token 数
- 每次调用记录延迟
- 可按 Run 查看 LLM 调用链

---

## 任务 6.4：Prometheus 指标

**目标**：暴露系统指标供 Grafana 展示。

**交付物**：
- `src/erp_copilot/observability/metrics.py`

**验收标准**：
- `GET /metrics` 返回 Prometheus 格式指标
- 包含：Run 创建数、完成数、失败数、各阶段延迟 P50/P95
- Worker 队列长度

---

## 任务 6.5：扩展评测数据集——200 条

**目标**：按 V6 设计文档完成完整评测集。

**交付物**：
- `evals/datasets/`（40 Tool Retrieval + 40 RAG + 50 Planning + 25 Recovery + 25 Security + 20 Failure）

**验收标准**：
- 6 类共 200 条，每条可独立执行
- 期望结果可量化比

---

## 任务 6.6：Eval Harness——自动化评测框架

**目标**：一键运行所有评测并输出报告。

**交付物**：
- `evals/harness.py`

**验收标准**：
- `uv run evals/run_all.py` 运行全部 200 条
- 输出每类得分和总体得分
- 失败案例有详细日志

---

## 任务 6.7：Record/Replay 模式

**目标**：录制真实 LLM 响应，CI 中重放（不调付费模型）。

**交付物**：
- `evals/record_replay.py`

**验收标准**：
- 录制模式保存 LLM 响应
- 重放模式用录制结果替代真实调用
- CI 中不调用付费模型也能跑评测

---

## 任务 6.8：负载测试——Locust

**目标**：压测系统并发能力。

**交付物**：
- `tests/performance/locustfile.py`

**验收标准**：
- 模拟 50 并发用户提交 Run
- 记录 P50/P95 延迟、吞吐量、错误率
- 输出 HTML 报告

---

## 任务 6.9：故障注入和恢复测试

**目标**：验证系统在异常情况下的行为。

**交付物**：
- `tests/performance/fault_injection.py`

**验收标准**：
- Worker 被 kill→任务恢复
- ERP 超时→正确重试或失败
- 订单创建不重复

---

## 任务 6.10：完成文档和演示

**目标**：补全项目文档和演示视频。

**交付物**：
- `docs/product.md`、`docs/architecture.md`、`docs/api-contracts.md`
- `docs/evaluation.md`、`docs/threat-model.md`、`docs/benchmark.md`
- `README.md`（含架构图、一键启动说明）

---

## 任务 6.11：清理和 Release v1.0

**目标**：清理敏感信息，打 Tag。

**交付物**：
- 无密钥、无日志、无大文件
- Git tag v1.0.0

---

## 任务 6.12：[REDACTED]表述和讲稿

**目标**：准备[REDACTED]材料。

**交付物**：
- 项目描述（含实测数字）
- 10 分钟讲解大纲
- 常见 Q&A

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
