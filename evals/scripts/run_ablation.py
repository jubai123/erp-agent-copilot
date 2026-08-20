"""Retrieval ablation study — compare three pipeline configurations.

Phase 3.13 — runs Vector-only, Hybrid, and Hybrid+Rerank against the
hand-annotated retrieval evaluation dataset, then outputs a comparison
table with Recall@K, MRR, and NDCG@K metrics.

Usage::

    uv run python evals/scripts/run_ablation.py
    uv run python evals/scripts/run_ablation.py --embedding-provider openai
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from sqlalchemy import text

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KB_ROOT = PROJECT_ROOT / "datasets" / "knowledge"
EVAL_FILE = PROJECT_ROOT / "datasets" / "eval" / "retrieval_queries.yaml"
TENANT_ID = "ablation-study"

# ---------------------------------------------------------------------------
# Embedding providers
# ---------------------------------------------------------------------------


class DummyEmbeddingProvider:
    """Returns deterministic 1536-dim vectors based on text content."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for t in texts:
            base = float(sum(ord(c) for c in t) % 10) / 10.0
            result.append([(base + i * 0.001) % 1.0 for i in range(1536)])
        return result


# Provider → (api_key_env, base_url_env, default_base_url, default_model) mapping.
# V5-compatible: discovers keys from system environment variables by checking
# multiple well-known env var names in priority order.
# Provider → (api_key_env, base_url_env, default_base_url, default_model) mapping.
# Ordered by priority: explicit LLM_API_KEY first, then embedding-capable providers.
_LLM_PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "openai": (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "https://api.openai.com/v1",
        "text-embedding-3-small",
    ),
    "dashscope": (
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "text-embedding-v4",
    ),
    "deepseek": (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com/v1",
        "text-embedding-3-small",
    ),
}


def _discover_llm_credentials() -> tuple[str, str, str]:
    for _provider, (key_env, url_env, default_url, default_model) in _LLM_PROVIDERS.items():
        api_key = os.getenv(key_env, "")
        if api_key:
            base_url = os.getenv(url_env, "") or default_url
            model = os.getenv("EMBEDDING_MODEL", "") or default_model
            return base_url, api_key, model
    return "", "", ""


def create_embedding_provider(provider_type: str) -> object:
    if provider_type == "dummy":
        return DummyEmbeddingProvider()

    if provider_type == "openai":
        base_url, api_key, model = _discover_llm_credentials()

        if not api_key:
            checked = ", ".join(v[0] for v in _LLM_PROVIDERS.values())
            raise RuntimeError(
                f"No API key found in environment. Checked: {checked}. "
                "Set one in your system environment or use --embedding-provider dummy."
            )

        from erp_copilot.retrieval.embedding import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(base_url=base_url, api_key=api_key, model=model)

    raise ValueError(f"Unknown embedding provider: {provider_type}")


class DummyReranker:
    """Dummy reranker — higher score for shorter documents (heuristic baseline)."""

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        scored = [(i, 1.0 / max(len(d), 1)) for i, d in enumerate(documents)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


# ---------------------------------------------------------------------------
# Knowledge base loading
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from Markdown content."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}

    end = 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1

    if end >= len(lines):
        return {}

    fm = yaml.safe_load("\n".join(lines[1:end]))
    return fm if isinstance(fm, dict) else {}


def load_knowledge_base() -> list[dict]:
    """Load all knowledge documents from the KB directory.

    Returns a list of dicts with keys: document_id, source_path, content.
    """
    docs = []
    for md_file in sorted(KB_ROOT.rglob("*.md")):
        # agent_skills/*/SKILL.md are skill-catalog definitions, not knowledge
        # documents — they carry no document_id and would all collide on stem
        # "SKILL", breaking the unique-id invariant.
        if "agent_skills" in md_file.parts:
            continue
        content = md_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        doc_id = fm.get("document_id", md_file.stem)
        docs.append(
            {
                "document_id": doc_id,
                "source_path": str(md_file.relative_to(KB_ROOT)),
                "content": content,
            }
        )
    return docs


def load_eval_queries() -> list[dict]:
    """Load evaluation queries from the YAML dataset."""
    raw = yaml.safe_load(EVAL_FILE.read_text(encoding="utf-8"))
    return raw["queries"]


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _test_database_url() -> str:
    """Ablation runs must only ever touch the dedicated test database."""
    return os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://copilot:copilot_dev@localhost:5432/erp_copilot_test",
    )


def _init_database() -> None:
    """Initialize database connection and create tables."""
    settings = Settings(
        database_url=_test_database_url(),
        llm_api_key="sk-test",
    )
    init_db(settings)

    import erp_copilot.domain.entities  # noqa: F401

    Base.metadata.create_all(get_engine())

    session = get_session()
    try:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        session.commit()
    finally:
        session.close()


def _ensure_tenant(session, tenant_id: str) -> None:
    slug = tenant_id.replace("-", "_")
    session.execute(
        text(
            "INSERT INTO tenants (id, name, slug, is_active, created_at, updated_at) "
            "VALUES (:id, :name, :slug, true, now(), now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": tenant_id, "name": tenant_id, "slug": slug},
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_knowledge_base(session, provider) -> dict[str, str]:
    """Ingest all KB docs and return a mapping of document_id → source."""
    from erp_copilot.domain.entities import KnowledgeDocument
    from erp_copilot.retrieval.embedding import embed_chunks
    from erp_copilot.retrieval.ingestion import hash_content, ingest_document
    from erp_copilot.retrieval.vector_store import store_embeddings

    _ensure_tenant(session, TENANT_ID)

    docs = load_knowledge_base()
    doc_id_to_source: dict[str, str] = {}

    for doc in docs:
        content_hash = hash_content(doc["content"])
        source = doc["document_id"]

        km = KnowledgeDocument(
            tenant_id=TENANT_ID,
            source=source,
            content_hash=content_hash,
            status="COMPLETED",
            version=1,
        )
        session.add(km)
        session.flush()

        chunks = ingest_document(doc["content"], source)
        embedded = embed_chunks(chunks, provider)
        store_embeddings(session, km.id, embedded)

        doc_id_to_source[source] = source

    session.commit()
    return doc_id_to_source


# ---------------------------------------------------------------------------
# Pipeline configurations
# ---------------------------------------------------------------------------


def search_vector_only(session, query_embedding: list[float], top_k: int) -> list[dict]:
    from erp_copilot.retrieval.vector_store import search_similar

    return search_similar(session, query_embedding, TENANT_ID, top_k=top_k)


def search_hybrid(session, query: str, query_embedding: list[float], top_k: int) -> list[dict]:
    from erp_copilot.retrieval.fts import keyword_search
    from erp_copilot.retrieval.hybrid import rrf_fuse
    from erp_copilot.retrieval.vector_store import search_similar

    vec_results = search_similar(session, query_embedding, TENANT_ID, top_k=top_k)
    kw_results = keyword_search(session, query, TENANT_ID, top_k=top_k)
    return rrf_fuse(vec_results, kw_results, k=60, top_k=top_k)


def search_hybrid_rerank(
    session, query: str, query_embedding: list[float], reranker, top_k: int
) -> list[dict]:
    from erp_copilot.retrieval.rerank import rerank_results

    hybrid = search_hybrid(session, query, query_embedding, top_k=top_k * 2)
    return rerank_results(query, hybrid, reranker, top_k=top_k)


def search_vector_rerank(
    session, query: str, query_embedding: list[float], reranker, top_k: int
) -> list[dict]:
    """Vector recall (2× candidates) followed by cross-encoder rerank.

    Counter-factual to :func:`search_hybrid_rerank`: no keyword channel,
    isolating whether the keyword search contributes anything beyond what
    vector recall + rerank already achieve.
    """
    from erp_copilot.retrieval.rerank import rerank_results

    vec = search_vector_only(session, query_embedding, top_k=top_k * 2)
    return rerank_results(query, vec, reranker, top_k=top_k)


# ---------------------------------------------------------------------------
# L1 Rule coverage analysis
# ---------------------------------------------------------------------------


def _load_l1_intent_map() -> dict[tuple[str, str], list[str]]:
    path = KB_ROOT / "rules" / "intent_rule_map.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {(e["domain"], e["action"]): e["rules"] for e in raw["intents"]}


# Manual mapping of eval queries to (domain, action) pairs for L1 coverage analysis.
_EVAL_QUERY_INTENTS: dict[str, tuple[str, str]] = {
    # -- Order operations --
    "如何创建订单": ("order", "create"),
    "订单有哪些状态": ("order", "query"),
    "怎么取消订单": ("order", "cancel"),
    "创建订单需要哪些参数": ("order", "create"),
    "订单的生命周期是怎样的": ("order", "query"),
    "如何查询订单信息": ("order", "query"),
    "订单取消有哪些限制条件": ("order", "cancel"),
    "订单创建后可以修改吗": ("order", "modify"),
    "什么情况下订单不能取消": ("order", "cancel"),
    "订单创建时的幂等机制": ("order", "create"),
    "订单金额是如何计算的": ("order", "create"),
    "如何处理重复提交的订单": ("order", "create"),
    # -- Product operations --
    "有哪些商品": ("product", "query"),
    "如何查询库存": ("product", "check_stock"),
    "库存不足怎么处理": ("product", "check_stock"),
    "如何按名称查询商品": ("product", "query"),
    "商品库存信息包括哪些字段": ("product", "check_stock"),
    "苹果的库存还有多少": ("product", "check_stock"),
    "商品目录包含哪些信息": ("product", "query"),
    "哪些商品按重量计价": ("product", "query"),
    # -- Supplier operations --
    "供应商不可用怎么办": ("supplier", "query"),
    "如何查询供应商信息": ("supplier", "query"),
    "供应商有哪些地区限制": ("supplier", "query"),
    "如何选择合适的供应商": ("supplier", "query"),
    "供应商的状态有哪些": ("supplier", "query"),
    "为什么供应商显示为不可用": ("supplier", "query"),
    "供应商的评分体系是怎样的": ("supplier", "query"),
    # -- Business rules & validation --
    "订单创建需要校验库存吗": ("order", "create"),
    "下单时如何验证供应商可用性": ("order", "create"),
    "订单可以跳过一个状态直接到终态吗": ("order", "query"),
    "什么样的订单需要审批": ("order", "create"),
    "什么时候需要调用审批流程": ("order", "cancel"),
    # -- Error & edge cases --
    "超时了怎么办": ("order", "query"),
    "库存不足的案例怎么处理": ("product", "check_stock"),
    "下单超时重试会不会重复创建订单": ("order", "create"),
    "同一个订单重复提交会怎样": ("order", "create"),
    "所有供应商都不可用时系统怎么处理": ("supplier", "query"),
    # -- Policies & security --
    "幂等键是什么": ("order", "create"),
    "需要审批的操作有哪些": ("order", "cancel"),
    "哪些接口需要幂等保护": ("order", "create"),
    "如何避免敏感数据泄露": ("security", "data_access"),
    "模拟器有哪些测试场景": ("system", "scenario"),
}


def analyze_l1_coverage(queries: list[dict]) -> dict:
    """Analyze L1 rule coverage for eval queries."""
    intent_map = _load_l1_intent_map()
    covered = 0
    details: list[dict] = []

    for q in queries:
        intent = _EVAL_QUERY_INTENTS.get(q["query"])
        if intent:
            rules = intent_map.get(intent, [])
            has_coverage = len(rules) > 0
            if has_coverage:
                covered += 1
            details.append(
                {
                    "query": q["query"],
                    "intent": f"{intent[0]}/{intent[1]}",
                    "l1_rules": rules,
                    "l1_covered": has_coverage,
                }
            )
        else:
            details.append(
                {
                    "query": q["query"],
                    "intent": "unknown",
                    "l1_rules": [],
                    "l1_covered": False,
                }
            )

    return {
        "coverage_rate": covered / len(queries) if queries else 0.0,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Metrics pipeline
# ---------------------------------------------------------------------------


def _extract_doc_ids(results: list[dict]) -> list[str]:
    """Extract unique document-level source IDs from retrieval results."""
    seen = set()
    ids = []
    for r in results:
        src = r["source"]
        if src not in seen:
            seen.add(src)
            ids.append(src)
    return ids


def evaluate_config(
    cfg_name: str,
    queries: list[dict],
    provider,
    run_fn,
) -> dict:
    """Run *run_fn* for each query and compute aggregate metrics."""
    from erp_copilot.retrieval.metrics import (
        mean_reciprocal_rank,
        ndcg_at_k,
        precision_at_k,
        recall_at_k,
    )

    session = get_session()
    try:
        metrics_list = []
        latencies = []
        per_query = []

        for q in queries:
            query_text = q["query"]
            relevant = set(q["relevant_docs"])
            query_embedding = provider.embed([query_text])[0]

            t0 = time.perf_counter()
            results = run_fn(session, query_text, query_embedding)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)

            retrieved_ids = _extract_doc_ids(results)

            m = {
                "recall@1": recall_at_k(retrieved_ids, relevant, k=1),
                "recall@5": recall_at_k(retrieved_ids, relevant, k=5),
                "mrr": mean_reciprocal_rank(retrieved_ids, relevant),
                "ndcg@5": ndcg_at_k(retrieved_ids, relevant, k=5),
                "precision@5": precision_at_k(retrieved_ids, relevant, k=5),
                "latency_ms": round(elapsed_ms, 2),
            }
            metrics_list.append(m)
            per_query.append(
                {
                    "query": query_text,
                    "relevant": list(relevant),
                    "retrieved": retrieved_ids,
                }
            )

        # Aggregate
        n = len(metrics_list)
        latencies_sorted = sorted(latencies)
        p50_idx = max(int(n * 0.5) - 1, 0)
        p95_idx = max(int(n * 0.95) - 1, 0)

        return {
            "config": cfg_name,
            "num_queries": n,
            "recall@1": round(sum(m["recall@1"] for m in metrics_list) / n, 4),
            "recall@5": round(sum(m["recall@5"] for m in metrics_list) / n, 4),
            "mrr": round(sum(m["mrr"] for m in metrics_list) / n, 4),
            "ndcg@5": round(sum(m["ndcg@5"] for m in metrics_list) / n, 4),
            "precision@5": round(sum(m["precision@5"] for m in metrics_list) / n, 4),
            "p50_latency_ms": round(latencies_sorted[p50_idx], 2),
            "p95_latency_ms": round(latencies_sorted[p95_idx], 2),
            "per_query": per_query,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_table(results: list[dict]) -> None:
    """Print a formatted comparison table."""
    headers = ["Config", "R@1", "R@5", "MRR", "NDCG@5", "P@5", "P50(ms)", "P95(ms)"]
    col_widths = [20, 8, 8, 8, 8, 8, 10, 10]

    sep = "+" + "+".join("-" * w for w in col_widths) + "+"
    header_line = "|" + "|".join(h.ljust(w) for h, w in zip(headers, col_widths, strict=True)) + "|"

    print("\n" + "=" * len(sep))
    print("  Retrieval Ablation Study")
    print("=" * len(sep))
    print(sep)
    print(header_line)
    print(sep)

    for r in results:
        row = [
            r["config"],
            f"{r['recall@1']:.4f}",
            f"{r['recall@5']:.4f}",
            f"{r['mrr']:.4f}",
            f"{r['ndcg@5']:.4f}",
            f"{r['precision@5']:.4f}",
            f"{r['p50_latency_ms']:.1f}",
            f"{r['p95_latency_ms']:.1f}",
        ]
        print("|" + "|".join(v.ljust(w) for v, w in zip(row, col_widths, strict=True)) + "|")

    print(sep)
    print(f"\n  {results[0]['num_queries']} queries evaluated per configuration.")
    print()


def _print_l1_report(l1_result: dict) -> None:
    """Print L1 rule coverage analysis."""
    print("=" * 60)
    print("  L1 Rule Coverage Analysis")
    print("=" * 60)
    print(
        f"  Coverage rate: {l1_result['coverage_rate']:.0%} "
        f"({sum(1 for d in l1_result['details'] if d['l1_covered'])}/"
        f"{len(l1_result['details'])} queries)\n"
    )

    for d in l1_result["details"]:
        status = "COVERED" if d["l1_covered"] else "UNCOVERED"
        print(f"  [{status}] {d['query']}")
        if d["intent"] != "unknown":
            print(f"          intent={d['intent']} rules={d['l1_rules']}")
        else:
            print("          intent=unknown → needs NL→intent mapping")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run retrieval ablation study")
    parser.add_argument("--top-k", type=int, default=10, help="Top-K for retrieval (default: 10)")
    parser.add_argument(
        "--skip-ingest", action="store_true", help="Skip KB ingestion (use existing data)"
    )
    parser.add_argument(
        "--embedding-provider",
        choices=["dummy", "openai"],
        default="dummy",
        help="Embedding provider: dummy (default) or openai",
    )
    parser.add_argument("--json", type=str, default=None, help="Save detailed results to JSON file")
    args = parser.parse_args()

    # 1. Initialize database
    print("Initializing database ...")
    _init_database()

    # 2. Create providers
    embedding_provider = create_embedding_provider(args.embedding_provider)
    jieba_note = " + jieba FTS" if args.embedding_provider == "dummy" else ""
    print(f"Embedding provider: {args.embedding_provider}{jieba_note}")

    if args.embedding_provider == "openai":
        from erp_copilot.retrieval.rerank import DashScopeReranker

        _, api_key, _ = _discover_llm_credentials()
        reranker = DashScopeReranker(api_key=api_key)
        print("Reranker: DashScope qwen3-rerank")
    else:
        reranker = DummyReranker()
        print("Reranker: Dummy (length-based heuristic)")

    # 3. Ingest knowledge base
    if not args.skip_ingest:
        print(f"Loading knowledge base from {KB_ROOT} ...")
        session = get_session()
        try:
            doc_map = ingest_knowledge_base(session, embedding_provider)
            print(f"  Ingested {len(doc_map)} documents.")
        finally:
            session.close()
    else:
        print("Skipping ingestion (--skip-ingest).")

    # 4. Load eval queries
    queries = load_eval_queries()
    print(f"Loaded {len(queries)} eval queries.")

    # 5. Run ablation — three configurations
    print("\nRunning ablation ...")
    print(f"  Config 1/3: Vector-only (top_k={args.top_k})")
    vec_result = evaluate_config(
        "Vector-only",
        queries,
        embedding_provider,
        lambda s, q, e: search_vector_only(s, e, args.top_k),
    )

    print(f"  Config 2/3: Hybrid (top_k={args.top_k})")
    hybrid_result = evaluate_config(
        "Hybrid", queries, embedding_provider, lambda s, q, e: search_hybrid(s, q, e, args.top_k)
    )

    print(f"  Config 3/3: Hybrid+Rerank (top_k={args.top_k})")
    hr_result = evaluate_config(
        "Hybrid+Rerank",
        queries,
        embedding_provider,
        lambda s, q, e: search_hybrid_rerank(s, q, e, reranker, args.top_k),
    )

    print(f"  Config 4/4: Vector+Rerank (top_k={args.top_k})")
    vr_result = evaluate_config(
        "Vector+Rerank",
        queries,
        embedding_provider,
        lambda s, q, e: search_vector_rerank(s, q, e, reranker, args.top_k),
    )

    # 5. L1 coverage analysis
    l1_result = analyze_l1_coverage(queries)

    # 6. Report
    all_results = [vec_result, hybrid_result, hr_result, vr_result]
    _print_table(all_results)
    _print_l1_report(l1_result)

    # 7. Optional JSON output
    if args.json:
        output = {
            "configs": [{k: v for k, v in r.items() if k != "per_query"} for r in all_results],
            "l1_coverage": {
                "coverage_rate": l1_result["coverage_rate"],
                "details": l1_result["details"],
            },
            "queries_used": len(queries),
            "per_query": {r["config"]: r["per_query"] for r in all_results},
        }
        json_str = json.dumps(output, indent=2, ensure_ascii=False)
        Path(args.json).write_text(json_str, encoding="utf-8")
        print(f"  Detailed results saved to {args.json}")


if __name__ == "__main__":
    main()
