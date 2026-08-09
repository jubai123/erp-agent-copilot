"""Security control evaluation — task 5.10.

A deterministic, guard-level evaluation of the security layer (docs/06): the
prompt-injection guard, the SSRF guard, and the output redactor are exercised
against the hand-annotated 25-case dataset (evals/datasets/security_25.json).
No LLM, no network, no database — SSRF DNS resolution is faked so every case
reproduces bit-for-bit.

Metrics (docs/10 task 5.10):
- 攻击拦截率 (interception rate) = intercepted attacks / attack cases
- 误报率 (false positive rate)   = false positives / benign cases

Usage::

    uv run python evals/scripts/run_security_eval.py
    uv run python evals/scripts/run_security_eval.py --report report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from erp_copilot.security.injection_guard import InjectionGuard
from erp_copilot.security.redaction import Redactor
from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_FILE = PROJECT_ROOT / "evals" / "datasets" / "security_25.json"

# Pre-registered egress policy: only https:443 to the two ERP hosts is allowed.
_SSRF_CONFIG = SSRFConfig(
    allowed_schemes=frozenset({"https"}),
    allowed_hosts=frozenset({"api.erp.example.com", "partner.erp.example.com"}),
    allowed_ports=frozenset({443}),
    trusted_internal_hosts=frozenset(),
)

# partner.erp.example.com answers with an RFC1918 address — a DNS rebinding
# attack against the internal network. api.erp.example.com answers publicly.
# Faking resolution keeps the eval offline and deterministic.
_FAKE_DNS: dict[str, list[str]] = {
    "api.erp.example.com": ["93.184.216.34"],
    "partner.erp.example.com": ["10.0.0.5"],
}


def fake_resolver(host: str) -> list[str]:
    """Deterministic stand-in for ``socket.getaddrinfo`` (offline eval)."""
    return list(_FAKE_DNS.get(host, []))


class Guards:
    """The three production guards under test, fully wired."""

    def __init__(self) -> None:
        self.injection = InjectionGuard()
        self.ssrf = SSRFGuard(_SSRF_CONFIG, resolve=fake_resolver)
        self.redactor = Redactor()


def load_cases() -> list[dict]:
    """Load the 25-case dataset from disk."""
    data = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    return data["cases"]


def intercept(guards: Guards, case: dict) -> bool:
    """True when the guard intercepted the input (blocked or redacted)."""
    layer = case["target_layer"]
    if layer == "injection_guard":
        return guards.injection.check(case["input"]).flagged
    if layer == "ssrf_guard":
        return not guards.ssrf.check(case["input"]).allowed
    if layer == "redactor":
        return guards.redactor.redact(case["input"]).count > 0
    raise ValueError(f"Unknown target_layer: {layer}")


def evaluate_cases(cases: list[dict], guards: Guards | None = None) -> list[dict]:
    """Run every case and record whether the guard matched the label."""
    guards = guards or Guards()
    outcomes: list[dict] = []
    for case in cases:
        intercepted = intercept(guards, case)
        is_attack = case["expected"] == "BLOCK"
        outcomes.append(
            {
                "case_id": case["case_id"],
                "target_layer": case["target_layer"],
                "attack_type": case["attack_type"],
                "expected": case["expected"],
                "intercepted": intercepted,
                "is_attack": is_attack,
                "correct": intercepted == is_attack,
            }
        )
    return outcomes


def summarize(outcomes: list[dict]) -> dict:
    """Aggregate interception rate and false positive rate over *outcomes*."""
    attack_total = sum(1 for o in outcomes if o["is_attack"])
    attack_intercepted = sum(1 for o in outcomes if o["is_attack"] and o["intercepted"])
    benign_total = len(outcomes) - attack_total
    false_positive = sum(1 for o in outcomes if not o["is_attack"] and o["intercepted"])
    return {
        "total_cases": len(outcomes),
        "attack_cases": attack_total,
        "benign_cases": benign_total,
        "intercepted_attacks": attack_intercepted,
        "false_positives": false_positive,
        "interception_rate": attack_intercepted / attack_total if attack_total else 0.0,
        "false_positive_rate": false_positive / benign_total if benign_total else 0.0,
        "correct_cases": sum(1 for o in outcomes if o["correct"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run security control eval")
    parser.add_argument("--report", type=str, default=None, help="JSON report output path")
    args = parser.parse_args()

    cases = load_cases()
    outcomes = evaluate_cases(cases)
    summary = summarize(outcomes)

    print(f"Security eval: {summary['total_cases']} cases")
    print(
        f"  攻击拦截率: {summary['interception_rate']:.0%} "
        f"({summary['intercepted_attacks']}/{summary['attack_cases']})"
    )
    print(
        f"  误报率:     {summary['false_positive_rate']:.0%} "
        f"({summary['false_positives']}/{summary['benign_cases']})"
    )
    for o in outcomes:
        mark = "OK  " if o["correct"] else "FAIL"
        print(
            f"  [{mark}] {o['case_id']} {o['target_layer']:<15} "
            f"expected={o['expected']:<5} intercepted={o['intercepted']}"
        )

    if args.report:
        payload = {"summary": summary, "outcomes": outcomes}
        Path(args.report).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  Report saved to {args.report}")


if __name__ == "__main__":
    main()
