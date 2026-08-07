# 架构决策记录（ADR）

> 本文档记录 V6 知识检索与 Agent Runtime 的关键架构取舍。每一决策包含：**决策**、**为什么**、**放弃了什么**、**评测如何验证**、**留的活口**（评测证明需要时才激活）。
>
> 更新原则：修改任一决策前，先在此记录原因，再改下游设计文档。

---

## 决策总览

| # | 决策点 | 最终选择 | 核心理由 |
|---|---|---|---|
| 1 | 知识检索触发 | **固定管道**，LLM 不决策"是否检索" | LLM 不知道自己不知道什么 |
| 2 | 知识分层 | **L1 Skill + L2 RAG 混合** | 规则类不该依赖概率召回 |
| 3 | 检索技术 | **pgvector 混合检索 + Rerank**，不用 GraphRAG | 事实查询 + 逐句引用 + 延迟 |
| 4 | 执行模式 | **Plan-then-Execute**（先完整 DAG 再执行） | 可并行、可恢复、可校验 |
| 5 | 工具候选 | **意图确定性过滤为主，向量检索按需精排** | 9 个工具时过滤已够，保留增长路径 |
| 6 | 正确性保障 | **四层纵深防御** | 单层无法防语义错选 |
| 7 | 检索融合 | **两段式（召回 + 重排）为唯一生产形态** | 消融证明裸混合是负收益，Rerank 是最大杠杆 |

一句话原则：**确定性优先，概率兜底**——能确定匹配的（意图过滤、L1 Skill、validate_plan）不交给概率（向量检索、LLM 自主），概率只在确定性覆盖不了的地方才上场。

---

## 决策一：知识检索触发 → 固定管道

**决策**：`retrieve_context` 是 LangGraph 的必走节点，位于 `classify_intent` 之后、`build_plan` 之前。不采用 ReAct 式"LLM 自主决定是否检索"。

```text
classify_intent → retrieve_context（必走）→ build_plan → ...
```

**为什么**：LLM 最危险的失败模式是"它以为自己知道，其实不知道"。若让 LLM 自主决定是否检索，它可能在简单的下单请求上直接编造业务流程，跳过外部知识。固定管道兜底了这种幻觉。

**放弃了什么**：多轮追问式检索的灵活性（查不到再换词查）。

**评测如何验证**：Context Recall、引用正确率、无答案拒答率。

**留的活口**：若 Phase 4 评测证明"验证时发现知识不足、需要再查一轮"，在 `verify_results` 后加条件边回退到 `retrieve_context`。默认不做，评测说需要才做。

---

## 决策二：知识分层 → L1 Skill + L2 RAG

**决策**：知识库分两层，注入同一 Prompt，L1 优先。

| 层 | 内容 | 注入方式 | 目标 |
|---|---|---|---|
| L1 Skill | 状态机规则、参数约束、审批策略、安全策略 | `classify_intent` 输出 → 意图→Skill 映射表 → **确定性注入** | 100% 命中（确定性匹配） |
| L2 RAG | API 文档、SOP 流程、业务案例、字段语义 | 意图 → 构造查询 → 混合检索 | Recall@5 ≥ 85%（概率召回） |

**为什么**：能写成"如果 A 则 B"的知识（状态机、参数格式、审批、安全）不应该依赖"这次检索召没召回"的概率。安全关键路径（审批、状态机校验）必须 100% 注入。

**放弃了什么**：单一 RAG 管道的统一维护模式——换来 L1 知识 100% 召回率与更简单的评测。

**评测如何验证**：L1 只测"LLM 是否遵循注入的约束"（不测召回，因为是确定性注入）；L2 测 Recall@5、MRR、NDCG。

**留的活口**：若 L2 检索成为主要失败来源且已有严格数据切分，才考虑微调 Embedding/Reranker。

---

## 决策三：检索技术 → pgvector 混合检索 + Rerank，不用 GraphRAG

**决策**：PostgreSQL FTS + pgvector 向量 → RRF 融合 → Cross-Encoder 重排。**明确拒绝 GraphRAG。**

**为什么不用 GraphRAG**：

| GraphRAG 设计目标 | V6 实际需要 |
|---|---|
| 成百上千文档的全局主题理解 | 15-25 份文档的事实查找 |
| 输出社区摘要 | 需要逐句引用（满足 Citation 评测） |
| 检索延迟数秒 | `retrieve_context` 是图中的一个节点，延迟直接叠加 |
| 索引成本数万次 LLM 调用 | 不可接受 |

**更根本的原因**：V6 的知识天然是结构化的（实体和关系本来就知道，无需 LLM 从非结构化文本"抽取"）。GraphRAG 的核心价值（自动建图）在这里没有用武之地。

**评测如何验证**：消融三组——Vector-only / Hybrid / Hybrid+Rerank，报 Recall@1/5、MRR、NDCG@5、P50/P95 延迟。

---

## 决策四：执行模式 → Plan-then-Execute

**决策**：`build_plan` 一次性生成完整 Plan DAG → `validate_plan` 确定性校验 → `policy_check` → 审批 → 按依赖拓扑执行（READ 并行、WRITE 串行）。执行阶段 LLM 零决策，只在 `verify_results` 介入。

**为什么**：先计划完整才能做并行、才能结构化校验、才能 Checkpoint 恢复。V5 的边计划边执行（while 循环）做不到这些。

**放弃了什么**：V5 边计划边执行的灵活性——换来可并行、可恢复、可审计。

**评测如何验证**：Plan valid rate、非法 DAG 拒绝率、写操作未审批次数 = 0。

---

## 决策五：工具候选 → 意图确定性过滤为主，向量检索按需精排

**决策**：`classify_intent` 的 `(domain, action)` → 确定性映射表 → 候选 3-8 个直接进 `build_plan` Prompt。候选数超过阈值（如 5-8 个）时才启用"向量粗筛 + Rerank 精排"。

```python
# 第一级：确定性过滤（主引擎，必过）——2026-08-07 与 candidate_filter.py 落地实现同步
DOMAIN_TOOL_MAP = {
    ("product", "query"): ["getProductByName", "getProductById", "getProductSubstitutesByName"],
    ("product", "check_stock"): ["getProductByName", "getProductById", "getProductSubstitutesByName"],
    ("supplier", "query"): ["querySuppliersByDeliveryRegion", "getSupplierByStatus"],
    ("order", "create"): ["getProductByName", "querySuppliersByDeliveryRegion", "getSupplierByStatus", "createOrder"],
    ("order", "query"): ["getOrderByOrderId"],
    ("order", "cancel"): ["getOrderByOrderId", "cancelOrder", "createOrder"],
    ("order", "update_status"): ["getOrderByOrderId", "updateOrderStatus"],
    ("order", "modify"): ["getOrderByOrderId", "cancelOrder", "createOrder"],
    ("security", "data_access"): [],  # 无工具候选
    ("system", "scenario"): [],       # 无工具候选
}

# 第二级：向量检索（按需精排，仅候选 > 阈值时启用；"超过阈值"为严格大于）
TOOL_RETRIEVAL_THRESHOLD = 5
def should_use_tool_retrieval(candidates, threshold=TOOL_RETRIEVAL_THRESHOLD) -> bool:
    return len(candidates) > threshold
```

**为什么**：V6 当前 9 个工具，意图过滤已足够，`build_plan` 犯错面是 3 选 1 不是 25 选 1。

**没有放弃 V5 工具检索**：两阶段思想（粗筛→精排）保留为增长路径，只是角色从"每次必走的唯一引擎"降级为"意图过滤后的按需精排层"。Milvus→pgvector，MongoDB→PostgreSQL，V5 意图训练数据转为 40 条 Tool 检索评测 Case。

**评测如何验证**：Tool Recall@5、MRR、混淆工具、hard negatives。

---

## 决策六：正确性保障 → 四层纵深防御

**决策**：Plan DAG 的正确性由四层叠加保证，不依赖任何一层单独保证。

```text
约束候选空间（上游，缩小 LLM 犯错面）
  → validate_plan 确定性校验（结构：未知Tool、Schema、依赖、环、补偿）
    → MCP Gateway 校验（执行：Scope、租户、SSRF、参数转换）
      → verify_results（业务：success_condition 匹配返回）
```

**为什么**：结构校验放行不了语义错选（"查苹果却选了 getProductById 传 id=苹果"），必须靠 `verify_results` 的 `success_condition` 兜底；而它是执行后才发现，所以又必须靠上游缩小候选空间，把"语义错选"概率压到评测可接受范围。

**评测如何验证**：40 条工具 Case + 语义错选回归集。

---

## 决策七：检索融合 → 两段式（召回 + 重排）为唯一生产形态

**决策**：生产检索管线固定为 **Hybrid → Rerank 两段式**（全文 + pgvector → RRF 融合 → Cross-Encoder 重排）。裸 Hybrid（不加重排）禁止作为最终配置。关键词通道保留，但它只有在重排器兜底精度时才引入价值。

**为什么**：Phase 3.13 消融实验（42 查询、DashScope `text-embedding-v4` 真实嵌入、`qwen3-rerank` 重排）：

| Config | R@1 | R@5 | MRR | NDCG@5 | P@5 | P50(ms) |
|---|---|---|---|---|---|---|
| Vector-only | 0.397 | 0.857 | 0.838 | 0.779 | 0.410 | 49 |
| Hybrid（裸融合） | 0.353 | **0.738** | 0.794 | 0.682 | 0.311 | 52 |
| Vector+Rerank | 0.357 | **0.917** | 0.798 | 0.805 | 0.407 | 217 |
| **Hybrid+Rerank** | 0.377 | **0.917** | **0.829** | **0.825** | **0.419** | 221 |

三个数据支撑的结论：

1. **Rerank 是最大杠杆**：Vector-only → Vector+Rerank 提升 R@5 +0.060、NDCG@5 +0.026，代价 +168ms P50。单一路径上没有任何改动比它收益更大。
2. **裸 Hybrid 是负收益**：OR 语义关键词检索高召回高噪声，直接 RRF 融合会稀释向量结果（R@5 0.86→0.74），且逐查询看"9 个查询关键词拖累、2 个双双受损"。两段式不是可选项，是前提。
3. **关键词通道的边际价值严格非负**：Vector+Rerank → Hybrid+Rerank 提升 MRR +0.031、NDCG@5 +0.020、R@1 +0.020，逐查询 NDCG **5 提升 / 37 持平 / 0 倒退**，成本仅 +4ms P50。关键词为排序质量贡献增量（把正确文档排到更前），且从不伤害结果。

**放弃了什么**：
- 裸 Hybrid 配置——指标上是四组中最差的。
- "只向量 + 重排"的简化管线——少了 0.02 NDCG 排序质量，关键词通道既然免费且不倒退，没有理由去掉。

**评测如何验证**：消融四组对比（Vector-only / Hybrid / Vector+Rerank / Hybrid+Rerank），报 Recall@1/5、MRR、NDCG@5、P50/P95 延迟。

**留的活口**：
- **已排除的担忧**：关键词通道"为空"不是失败信号。逐查询分析（42 条中 3 条关键词为空：敏感数据泄露 / 商品目录 / 幂等保护）证明两段式管线全部兜底，R@5 均为 1.0——前两条靠向量通道本身即可，幂等保护靠 Rerank 把 `api-order-create` 从召回池提进 top-5（该条 Vector-only 仅 0.50）。关键词为空时 RRF 退化为纯向量结果，无副作用。**LLM 查询重写不启用**（省每查询 +200~500ms）。触发条件收紧为：仅当后续评测出现"关键词为空 **且** 相关文档落在向量召回池之外"的查询（两段式都救不回）才需要术语对齐，先量化再上。
- RRF 融合参数（关键词 top_k、k 值）可调，前提是消融数据证明裸 Hybrid 不再劣化于 Vector-only。
- 已知 `ndcg_at_k` 签名 `(retrieved_ids, relevant_ids, graded_relevance=None, k=None)` 位置参数易踩坑（`k` 在 `graded_relevance` 之后），调用需用关键字参数，后续可考虑重排签名。

---

## 落地顺序（Phase 3 启动）

```text
1. 建立统一事实表          ← 对齐模拟器世界状态，先于一切文档
2. 编写 L2 文档（15-25 份）  ← domain-model → api-guides → rules → processes → cases
3. 定义 L1 Skill（10-15 个） ← 状态机、参数约束、审批策略，配意图→Skill 映射表
4. 编写 40 检索评测问题     ← 每文档 2-3 正 + 1 混淆负
5. 实现检索管线             ← ingestion → chunk → FTS+pgvector → RRF → rerank
6. 跑消融三组基线           ← Vector-only / Hybrid / Hybrid+Rerank
7. 预留工具精排接口         ← 不提前实现，接口留好
```
