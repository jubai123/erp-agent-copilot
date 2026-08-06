"""Per-query analysis of ablation results — recall@5 patterns across configs."""

from __future__ import annotations

import collections
import json
import sys


def recall_at_5(retrieved: list[str], relevant: set[str]) -> float:
    from erp_copilot.retrieval.metrics import recall_at_k

    ids: list[str] = []
    seen: set[str] = set()
    for r in retrieved:
        if r not in seen:
            seen.add(r)
            ids.append(r)
    return recall_at_k(ids, relevant, k=5)


def _dedup(retrieved: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in retrieved:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def main(path: str) -> None:
    data = json.load(open(path, encoding="utf-8"))
    pq = data["per_query"]
    idx = {q["query"]: i for i, q in enumerate(pq["Vector-only"])}

    stats: collections.Counter = collections.Counter()
    print(f"{'Query':<26} {'V@5':>5} {'H@5':>5} {'HR@5':>5}   pattern")
    for q in pq["Vector-only"]:
        qtext = q["query"]
        v = recall_at_5(pq["Vector-only"][idx[qtext]]["retrieved"], set(q["relevant"]))
        h = recall_at_5(pq["Hybrid"][idx[qtext]]["retrieved"], set(pq["Hybrid"][idx[qtext]]["relevant"]))
        hr = recall_at_5(pq["Hybrid+Rerank"][idx[qtext]]["retrieved"], set(pq["Hybrid+Rerank"][idx[qtext]]["relevant"]))

        if h < v and hr >= v:
            pat = "keyword hurt, rerank rescued"
            stats["rescue"] += 1
        elif h > v:
            pat = "keyword helped"
            stats["kw_help"] += 1
        elif hr > v and h == v:
            pat = "rerank only"
            stats["rerank_only"] += 1
        elif h < v and hr < v:
            pat = "both hurt"
            stats["both_hurt"] += 1
        elif h < v:
            pat = "hybrid hurt"
            stats["hybrid_hurt"] += 1
        elif hr > v:
            pat = "rerank helped"
            stats["rerank_helped"] += 1
        else:
            pat = "no change"
            stats["no_change"] += 1
        print(f"{qtext:<26} {v:>5.2f} {h:>5.2f} {hr:>5.2f}   {pat}")

    print()
    print("Pattern counts:", dict(stats))

    # --- Vector+Rerank vs Hybrid+Rerank: isolate keyword marginal value ---
    if "Vector+Rerank" not in pq:
        return
    from erp_copilot.retrieval.metrics import mean_reciprocal_rank, ndcg_at_k

    stats2: collections.Counter = collections.Counter()
    ndcg_deltas = []
    print("\nNDCG@5 (Hybrid+Rerank − Vector+Rerank), only non-zero deltas:")
    for q in pq["Vector-only"]:
        qtext = q["query"]
        rel = set(q["relevant"])
        v_ret = _dedup(pq["Vector+Rerank"][idx[qtext]]["retrieved"])
        h_ret = _dedup(pq["Hybrid+Rerank"][idx[qtext]]["retrieved"])
        v_n = ndcg_at_k(v_ret, rel, k=5)
        h_n = ndcg_at_k(h_ret, rel, k=5)
        d = h_n - v_n
        ndcg_deltas.append(d)
        if abs(d) > 0.001:
            pat = "HR better" if d > 0 else "VR better"
            stats2[pat] += 1
            print(f"  {qtext:<26} {v_n:>5.2f} {h_n:>5.2f} {d:>+6.2f}  {pat}")

    tied = 42 - stats2.get("HR better", 0) - stats2.get("VR better", 0)
    print(f"\nNDCG@5 keyword marginal value: HR better={stats2.get('HR better', 0)}, "
          f"VR better={stats2.get('VR better', 0)}, tied={tied}")
    print(f"  mean NDCG@5 delta (Hybrid+Rerank − Vector+Rerank): {sum(ndcg_deltas) / len(ndcg_deltas):+.4f}")


if __name__ == "__main__":
    main(sys.argv[1])
