"""Diagnostic: investigate why keyword search contributes zero to hybrid retrieval.

Answers: Does keyword_search() return results? If not, why?
"""

from __future__ import annotations

import json
import os

import jieba
import yaml
from dotenv import load_dotenv
from sqlalchemy import text

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import get_session, init_db

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL_FILE = os.path.join(PROJECT_ROOT, "datasets", "eval", "retrieval_queries.yaml")
TENANT_ID = "ablation-study"


def main() -> None:
    settings = Settings(
        database_url="postgresql://copilot:copilot_dev@localhost:5432/erp_copilot",
        llm_api_key="sk-test",
    )
    init_db(settings)

    queries = yaml.safe_load(open(EVAL_FILE, encoding="utf-8"))["queries"]
    session = get_session()

    try:
        # Check basic stats
        total = session.execute(
            text("SELECT count(*) FROM document_chunks dc "
                 "JOIN knowledge_documents kd ON kd.id = dc.document_id "
                 "WHERE kd.tenant_id = :tid"),
            {"tid": TENANT_ID},
        ).scalar()
        print(f"Total chunks for tenant '{TENANT_ID}': {total}")

        # Check search_vector population
        null_sv = session.execute(
            text("SELECT count(*) FROM document_chunks dc "
                 "JOIN knowledge_documents kd ON kd.id = dc.document_id "
                 "WHERE kd.tenant_id = :tid AND dc.search_vector IS NULL"),
            {"tid": TENANT_ID},
        ).scalar()
        print(f"Chunks with NULL search_vector: {null_sv}")

        # Show sample search_vectors
        sample = session.execute(
            text("SELECT dc.id, dc.content, dc.search_vector FROM document_chunks dc "
                 "JOIN knowledge_documents kd ON kd.id = dc.document_id "
                 "WHERE kd.tenant_id = :tid LIMIT 3"),
            {"tid": TENANT_ID},
        ).mappings().all()
        print("\nSample search_vectors:")
        for r in sample:
            print(f"  chunk={r['id']}")
            print(f"  content[:100]={r['content'][:100]}")
            print(f"  search_vector={r['search_vector']}")
            print()

        # Now test keyword search per query
        print("=" * 80)
        print("Per-query keyword search diagnosis:")
        print("=" * 80)

        empty_count = 0
        for q in queries:
            query_text = q["query"]
            segmented = " ".join(jieba.cut(query_text.strip()))

            # Run actual keyword_search
            from erp_copilot.retrieval.fts import keyword_search

            results = keyword_search(session, query_text, TENANT_ID, top_k=10)

            # Also try manual query with OR semantics
            tokens = segmented.split()
            if tokens:
                or_query = " | ".join(tokens)
                or_results = session.execute(
                    text("""
                        SELECT dc.id, kd.source
                        FROM document_chunks dc
                        JOIN knowledge_documents kd ON kd.id = dc.document_id
                        WHERE kd.tenant_id = :tid
                          AND dc.search_vector @@ to_tsquery('simple', :q)
                        LIMIT 10
                    """),
                    {"tid": TENANT_ID, "q": or_query},
                ).mappings().all()
            else:
                or_results = []

            if not results:
                empty_count += 1
                print(f"\nEMPTY: '{query_text}'")
                print(f"  jieba tokens: {segmented}")
                plain_tsquery = "plainto_tsquery('simple', '{}')".format(segmented.replace("'", "''"))
                print(f"  tsquery: {plain_tsquery}")

                # Show tokens that exist in any document
                for token in tokens[:10]:
                    cnt = session.execute(
                        text("SELECT count(*) FROM document_chunks dc "
                             "JOIN knowledge_documents kd ON kd.id = dc.document_id "
                             "WHERE kd.tenant_id = :tid "
                             "AND dc.search_vector @@ to_tsquery('simple', :t)"),
                        {"tid": TENANT_ID, "t": token},
                    ).scalar()
                    print(f"    token '{token}' exists in {cnt} chunks")

                if or_results:
                    print(f"  OR-mode would find {len(or_results)} results:")
                    for r in or_results[:5]:
                        print(f"    {r['source']} (chunk={r['id'][:8]}...)")

        if empty_count == 0:
            print("\nAll queries returned keyword results! Checking overlap...")
            # Compare semantic vs keyword result sets
            from erp_copilot.retrieval.embedding import OpenAIEmbeddingProvider

            base_url, api_key, model = _discover()
            provider = OpenAIEmbeddingProvider(base_url=base_url, api_key=api_key, model=model)

            for q in queries[:5]:
                query_text = q["query"]
                emb = provider.embed([query_text])[0]

                from erp_copilot.retrieval.vector_store import search_similar
                from erp_copilot.retrieval.fts import keyword_search

                vec = search_similar(session, emb, TENANT_ID, top_k=10)
                kw = keyword_search(session, query_text, TENANT_ID, top_k=10)

                vec_ids = {r["chunk_id"] for r in vec}
                kw_ids = {r["chunk_id"] for r in kw}
                overlap = vec_ids & kw_ids
                kw_only = kw_ids - vec_ids

                print(f"\n'{query_text}':")
                print(f"  vec={len(vec_ids)} chunks, kw={len(kw_ids)} chunks")
                print(f"  overlap={len(overlap)}, kw_only={len(kw_only)}")

        print(f"\nSummary: {empty_count}/{len(queries)} queries returned empty keyword results")

    finally:
        session.close()


def _discover() -> tuple[str, str, str]:
    for key_env, url_env, default_url, default_model in [
        ("LLM_API_KEY", "LLM_BASE_URL", "https://api.openai.com/v1", "text-embedding-3-small"),
        ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL",
         "https://dashscope.aliyuncs.com/compatible-mode/v1", "text-embedding-v4"),
        ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
         "https://api.deepseek.com/v1", "text-embedding-3-small"),
    ]:
        api_key = os.getenv(key_env, "")
        if api_key:
            base_url = os.getenv(url_env, "") or default_url
            model = os.getenv("EMBEDDING_MODEL", "") or default_model
            return base_url, api_key, model
    return "", "", ""


if __name__ == "__main__":
    main()
