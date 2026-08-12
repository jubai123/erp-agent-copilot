"""One-click evaluation harness — task 6.6.

Wires the six category runners into evals.harness.run_all and emits a
per-category + overall score report with a detailed failure log.

Runner provenance is recorded per category so a number is never mistaken for
what it is not:

- ``deterministic`` — real production logic, offline and bit-reproducible:
  tool_retrieval uses candidate_filter's first-level DOMAIN_TOOL_MAP filter;
  security uses the real guards with faked DNS resolution; planning runs the
  real classify → build_plan → validate_plan node chain with the worker's
  tool schemas; recover_or_replan runs the real recover_or_replan node
  (hard-wired in build_agent_graph) on each seeded AgentState snapshot.
- ``retrieval_pipeline`` — the real RAG chain (embed → vector → FTS → RRF →
  rerank) against the dedicated test database, using deterministic providers
  so it stays offline and reproducible. Requires a local pgvector database;
  the knowledge base is ingested idempotently from datasets/knowledge.

Usage::

    uv run evals/run_all.py
    uv run evals/run_all.py --report evals/reports/report.json
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

import yaml

from erp_copilot.tools.candidate_filter import filter_candidates
from evals.harness import RunnerFn, RunnerOutput, format_report, run_all, write_report
from evals.recover_or_replan_eval import evaluate_cases as evaluate_recover_or_replan
from evals.scorers.retrieval_scorer import score_retrieved
from evals.scripts.run_security_eval import Guards, evaluate_cases, summarize

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _tool_retrieval(cases: list[dict]) -> RunnerOutput:
    """Deterministic: does the first-level DOMAIN_TOOL_MAP filter surface the expected tool?"""
    per_case: list[dict] = []
    for case in cases:
        retrieved = filter_candidates(case["domain"], case["action"])
        expected = case["expected_tool"]
        passed = expected in retrieved
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected,
                "actual": retrieved,
                "detail": (
                    "" if passed else f"expected {expected!r} not in filter output {retrieved!r}"
                ),
            }
        )
    n = len(per_case)
    recall = sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0
    return {
        "mode": "deterministic",
        "primary_score": recall,
        "metrics": {
            "recall_at_filter": recall,
            "avg_candidates": round(sum(len(pc["actual"]) for pc in per_case) / n, 2) if n else 0.0,
        },
        "per_case": per_case,
    }


KB_ROOT = Path(__file__).resolve().parent.parent / "datasets" / "knowledge"
_EVAL_TENANT = "knowledge-rag-eval"


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a Markdown document."""
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


def _load_knowledge_docs() -> list[dict]:
    """Load every knowledge document under datasets/knowledge."""
    docs: list[dict] = []
    for md_file in sorted(KB_ROOT.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        docs.append(
            {
                "document_id": fm.get("document_id", md_file.stem),
                "content": content,
            }
        )
    return docs


def _ensure_eval_tenant(session) -> str:
    """Create the eval tenant if missing and return its id."""
    from erp_copilot.domain.entities import Tenant

    tenant = session.query(Tenant).filter_by(name=_EVAL_TENANT).first()
    if tenant is None:
        tenant = Tenant(name=_EVAL_TENANT, slug=_EVAL_TENANT.replace("-", "_"))
        session.add(tenant)
        session.commit()
    return tenant.id


def _ensure_kb_ingested(session, provider, tenant_id: str) -> None:
    """Idempotently ingest datasets/knowledge into *tenant_id*.

    Content-hash dedup makes re-runs no-ops, matching the Phase 3.4 version
    management semantics of the ingestion pipeline.
    """
    from erp_copilot.domain.entities import KnowledgeDocument
    from erp_copilot.retrieval.embedding import embed_chunks
    from erp_copilot.retrieval.ingestion import hash_content, ingest_document
    from erp_copilot.retrieval.vector_store import store_embeddings

    existing = {
        d.content_hash
        for d in session.query(KnowledgeDocument).filter_by(tenant_id=tenant_id).all()
    }
    for doc in _load_knowledge_docs():
        content_hash = hash_content(doc["content"])
        if content_hash in existing:
            continue
        km = KnowledgeDocument(
            tenant_id=tenant_id,
            source=doc["document_id"],
            content_hash=content_hash,
            status="COMPLETED",
            version=1,
        )
        session.add(km)
        session.flush()
        chunks = ingest_document(doc["content"], doc["document_id"])
        embedded = embed_chunks(chunks, provider)
        store_embeddings(session, km.id, embedded)
    session.commit()


def _retrieve_sources(session, query: str, tenant_id: str, provider, reranker) -> list[str]:
    """Return deduped document sources retrieved by the real pipeline."""
    from erp_copilot.retrieval.pipeline import search_knowledge

    results = search_knowledge(
        session,
        query,
        tenant_id,
        top_k=5,
        embedding_provider=provider,
        reranker=reranker,
    )
    seen: set[str] = set()
    sources: list[str] = []
    for r in results:
        src = r["source"]
        if src not in seen:
            seen.add(src)
            sources.append(src)
    return sources


def _knowledge_rag(cases: list[dict]) -> RunnerOutput:
    """Real retrieval pipeline against the dedicated test database.

    The knowledge base is ingested idempotently from datasets/knowledge and
    every query is answered by the real embed → vector → FTS → RRF → rerank
    chain with deterministic providers (offline). Answerable queries pass when
    at least one relevant document is retrieved in the top 5; refusal queries
    pass only when no keyword hits exist, so an out-of-domain query that
    happens to share a vocabulary word with the KB (e.g. "供应商") is honestly
    scored as a miss.
    """
    import sqlalchemy as sa

    from erp_copilot.infrastructure.config import Settings
    from erp_copilot.infrastructure.database import (
        Base,
        get_engine,
        get_session,
        init_db,
    )
    from erp_copilot.retrieval.pipeline import (
        DeterministicEmbeddingProvider,
        DeterministicReranker,
    )

    url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://copilot:copilot_dev@localhost:5432/erp_copilot_test",
    )
    settings = Settings(database_url=url, llm_api_key="")
    init_db(settings)
    import erp_copilot.domain.entities  # noqa: F401

    session = get_session()
    try:
        session.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.commit()
    finally:
        session.close()
    Base.metadata.create_all(get_engine())

    provider = DeterministicEmbeddingProvider()
    reranker = DeterministicReranker()
    session = get_session()
    try:
        tenant_id = _ensure_eval_tenant(session)
        _ensure_kb_ingested(session, provider, tenant_id)

        from erp_copilot.retrieval.fts import keyword_search

        per_case: list[dict] = []
        answerable: list[dict] = []
        total_refusals = 0
        refused = 0
        for case in cases:
            query = case["query"]
            retrieved = _retrieve_sources(session, query, tenant_id, provider, reranker)
            if case["should_answer"]:
                expected = case["relevant_docs"]
                passed = any(doc in retrieved for doc in expected)
                answerable.append(score_retrieved(retrieved, expected, k=5))
                per_case.append(
                    {
                        "case_id": case["case_id"],
                        "passed": passed,
                        "expected": expected,
                        "actual": retrieved,
                        "detail": "" if passed else f"no relevant doc in {retrieved!r}",
                    }
                )
            else:
                total_refusals += 1
                kw_hits = keyword_search(session, query, tenant_id, top_k=5)
                passed = not kw_hits
                if passed:
                    refused += 1
                per_case.append(
                    {
                        "case_id": case["case_id"],
                        "passed": passed,
                        "expected": [],
                        "actual": retrieved,
                        "detail": "" if passed else f"refused query retrieved {retrieved!r}",
                    }
                )
    finally:
        session.close()

    n = len(per_case)
    answerable_n = len(answerable)
    recall = sum(m["recall@5"] for m in answerable) / answerable_n if answerable_n else 0.0
    ndcg = sum(m["ndcg@5"] for m in answerable) / answerable_n if answerable_n else 0.0
    return {
        "mode": "retrieval_pipeline",
        "primary_score": sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0,
        "metrics": {
            "recall@5": round(recall, 4),
            "ndcg@5": round(ndcg, 4),
            "answerable_cases": answerable_n,
            "refusal_accuracy": refused / total_refusals if total_refusals else 0.0,
        },
        "per_case": per_case,
    }


def _planning(cases: list[dict]) -> RunnerOutput:
    """Real agent node chain: classify_intent → build_plan → validate_plan.

    Runs the exact nodes the worker graph executes (graph_builder's
    build_deterministic_plan_node and build_validate_plan_node with the real
    WORKER_TOOL_SCHEMAS). A case passes only when all four hold:

    - the planned tool sequence matches the dataset's expected steps;
    - every expected parameter is covered by step.arguments or a step source;
    - the plan passes structural validation (required params, no cycles,
      WRITE compensation notes);
    - every WRITE step carries the run-scoped stamped idempotency key.

    mode stays "deterministic" — these are the real production nodes, offline
    and bit-reproducible.
    """
    from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS
    from erp_copilot.agent.nodes.classify_intent import classify_intent_node
    from erp_copilot.agent.nodes.validate_plan import build_validate_plan_node
    from erp_copilot.agent.planner import build_deterministic_plan_node
    from erp_copilot.agent.state import AgentState
    from erp_copilot.domain.enums import ToolRiskLevel

    plan_node = build_deterministic_plan_node()
    validate_node = build_validate_plan_node(tool_schemas=WORKER_TOOL_SCHEMAS)

    per_case: list[dict] = []
    multi_step = 0
    stamped = 0
    for case in cases:
        state = AgentState(run_id=f"eval-{case['case_id']}", tenant_id="eval", query=case["query"])
        state = state.model_copy(update=classify_intent_node(state))
        state = state.model_copy(update=plan_node(state))
        state = state.model_copy(update=validate_node(state))

        expected = [step["tool"] for step in case["steps"]]
        plan = state.plan
        actual = [step.tool_name for step in plan.steps] if plan else []
        failures: list[str] = []
        if actual != expected:
            failures.append(f"plan {actual!r} != expected {expected!r}")
        else:
            # actual == expected and the dataset's steps are never empty, so
            # the plan node must have produced a plan here.
            assert plan is not None
            for exp_step, act_step in zip(case["steps"], plan.steps, strict=True):
                missing = [
                    p
                    for p in exp_step.get("params", {})
                    if p not in act_step.arguments and p not in act_step.argument_sources
                ]
                if missing:
                    failures.append(f"step {act_step.step_id} missing params {missing!r}")
        if state.plan_validation is not None and not state.plan_validation.is_valid:
            failures.append(
                "validate: " + ", ".join(e.code for e in state.plan_validation.errors)
            )
        if state.plan is not None:
            stamped += sum(
                1
                for s in state.plan.steps
                if s.risk_level == ToolRiskLevel.WRITE and s.idempotency_key is not None
            )
            unstamped = [
                s.step_id
                for s in state.plan.steps
                if s.risk_level == ToolRiskLevel.WRITE and s.idempotency_key is None
            ]
            if unstamped:
                failures.append(f"WRITE steps without idempotency key: {unstamped!r}")

        passed = not failures
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected,
                "actual": actual,
                "detail": "" if passed else "; ".join(failures),
            }
        )
        if not case["single_step"]:
            multi_step += 1
    n = len(per_case)
    valid = sum(1 for pc in per_case if pc["passed"])
    return {
        "mode": "deterministic",
        "primary_score": valid / n if n else 0.0,
        "metrics": {
            "plan_valid_rate": valid / n if n else 0.0,
            "multi_step_count": multi_step,
            "write_steps_with_idempotency_key": stamped,
        },
        "per_case": per_case,
    }


def _recover_or_replan(cases: list[dict]) -> RunnerOutput:
    """Deterministic: run the real recover_or_replan node on each seeded state.

    Delegates to evals/recover_or_replan_eval.py, which maps each case's seed
    onto an AgentState snapshot and calls recover_or_replan — the recovery node
    hard-wired in build_agent_graph — asserting the terminal state.
    """
    return evaluate_recover_or_replan(cases)


def _security(cases: list[dict]) -> RunnerOutput:
    """Deterministic: run the real guards (faked DNS) against the dataset."""
    outcomes = evaluate_cases(cases, Guards())
    per_case: list[dict] = []
    for outcome in outcomes:
        intercepted = outcome["intercepted"]
        actual = "BLOCK" if intercepted else "ALLOW"
        per_case.append(
            {
                "case_id": outcome["case_id"],
                "passed": outcome["correct"],
                "expected": outcome["expected"],
                "actual": actual,
                "detail": (
                    ""
                    if outcome["correct"]
                    else f"guard returned {actual}, expected {outcome['expected']}"
                ),
            }
        )
    n = len(per_case)
    correct = sum(1 for pc in per_case if pc["passed"])
    summary = summarize(outcomes)
    return {
        "mode": "deterministic",
        "primary_score": correct / n if n else 0.0,
        "metrics": {
            "interception_rate": summary["interception_rate"],
            "false_positive_rate": summary["false_positive_rate"],
            "correct_rate": correct / n if n else 0.0,
        },
        "per_case": per_case,
    }


CATEGORY_RUNNERS: dict[str, RunnerFn] = {
    "tool_retrieval": _tool_retrieval,
    "knowledge_rag": _knowledge_rag,
    "planning": _planning,
    "recover_or_replan": _recover_or_replan,
    "security": _security,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the 200-case eval harness")
    parser.add_argument(
        "--report",
        type=str,
        default=str(REPORT_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"),
        help="JSON report output path",
    )
    args = parser.parse_args()

    report = run_all(CATEGORY_RUNNERS)
    print(format_report(report))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, report_path)
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
