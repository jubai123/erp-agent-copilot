# 知识库与检索设计

## 1. 知识库边界

知识库保存稳定、可版本化、需要引用的ERP业务知识：

- 商品、库存、供应商和订单字段语义。
- ERP API说明、参数约束和返回值解释。
- 库存校验、供应商选择和下单业务规则。
- 订单状态机、合法状态转换和异常处理流程。
- 跨系统业务流程SOP和历史业务案例。
- 创建、更新、取消订单的审批规范。
- 权限、安全和敏感数据处理规则。

实时库存、供应商状态、商品价格和订单状态必须通过ERP Tool查询，不作为向量库中的当前事实。

## 2. 导入流程

```text
提交导入任务
  → 文件类型和大小校验
  → 病毒与敏感信息检查
  → 内容解析
  → 规范化和去重
  → 元数据校验
  → Chunk切分
  → Embedding
  → 全文索引和向量索引
  → 质量检查
  → 发布新版本
```

导入任务必须可查询状态和失败原因。导入失败不能留下部分可检索的新版本。

## 3. 文档元数据

```json
{
  "document_id": "process-create-order",
  "tenant_id": "demo-tenant",
  "document_type": "business_process",
  "title": "库存校验与订单创建流程",
  "business_domains": ["product", "supplier", "order"],
  "environment": "demo",
  "version": "1.2",
  "owner": "erp-platform",
  "trust_level": "trusted",
  "access_scope": "knowledge:erp:read",
  "source_uri": "repo://knowledge/processes/create-order.md",
  "effective_from": "2026-07-01T00:00:00Z",
  "effective_to": null,
  "checksum": "sha256:..."
}
```

`trust_level`建议包含：

- `trusted`：内部批准并可用于回答。
- `untrusted`：外部导入，只能作为待验证材料。
- `quarantined`：检测到攻击或格式问题，不进入检索结果。

## 4. Chunk设计

优先按Markdown标题、列表和语义段落切分，再对过长段落使用Token窗口。

每个Chunk保存：

- `chunk_id`、`document_id`和文档版本。
- 标题路径和章节名称。
- 原始文本和规范化文本。
- 业务域、环境、文档类型和ACL。
- Token数量、序号和相邻Chunk ID。
- 全文检索字段、Embedding和校验和。

建议初始目标为每个Chunk 300至600个中文Token，Overlap控制在50至100个Token。最终参数必须通过评测确定。

## 5. 混合检索

```text
Query规范化
  → 服务和时间实体提取
  → PostgreSQL全文检索
  → pgvector向量检索
  → 分数归一化和融合
  → ACL与有效期过滤
  → Reranker
  → 去重与上下文装配
```

检索结果至少返回：

- `chunk_id`
- 文档标题和章节
- 文档版本
- 原始来源
- 全文、向量、融合和Rerank分数
- 用于生成的文本

## 6. Query处理

- 保留原始Query，任何改写都要记录。
- 从Query提取业务域、产品、数量、地区、订单ID和动作作为过滤条件。
- 对口语化或多轮问题可以生成检索改写，但不能改变业务意图。
- 对无答案问题保留拒答路径，不强制召回低相关内容。

## 7. 引用与回答约束

最终答案中的事实性结论必须关联：

- 知识文档引用，或
- Tool执行证据，或
- 明确标注为推测。

引用格式建议包含文档标题、章节、版本和Chunk ID。Verifier检查关键结论是否有支持证据，并检测引用文本是否真的包含该事实。

## 8. RAG安全

- 系统提示明确知识内容是数据，不是指令。
- 检索前执行租户、ACL、版本和可信度过滤。
- 检索后检测可疑指令、密钥模式和越权内容。
- 隔离恶意测试文档并生成`security_event`。
- 不允许文档内容生成未注册Tool或绕过审批。
- 输出前进行Secret和PII脱敏。

## 9. 评测与消融

至少比较三组：

1. Vector-only。
2. 全文与Vector混合检索。
3. Hybrid + Reranker。

报告以下指标：

- Recall@1、Recall@5。
- MRR、NDCG@5。
- Context Recall。
- 引用正确率。
- 无答案拒答率。
- 检索P50和P95延迟。

微调只有在检索仍是主要失败来源且已有严格数据切分时才启动。
