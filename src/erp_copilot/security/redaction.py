"""Output-layer secret and PII redaction — task 5.5.

A pure, deterministic output transformer (docs/06 §5 output layer): scans text
for secret and PII patterns and rewrites them in place — API keys become
``[REDACTED]``, Chinese mobile numbers and ID card numbers keep only their
edges. Regular expressions keep the behaviour deterministic and auditable,
consistent with the project's determinism-first security stance (docs/06 §1).

Unlike the guards (:mod:`erp_copilot.security.ssrf_guard`,
:mod:`erp_copilot.security.injection_guard`), redaction is not an interception
point: it runs on every outbound answer, rewrites text, and touches no
database — the result carries the redacted text and a replacement count so the
caller decides whether anything needs logging. That keeps this module
framework-free and trivially unit-testable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionRule:
    """One secret/PII family: a stable name, a pattern, and a replacement."""

    name: str
    pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str]


@dataclass(frozen=True)
class RedactionResult:
    """Outcome of a redaction pass over one text."""

    redacted: str
    count: int
    matched_rules: tuple[str, ...] = ()


def _mask_keep_edges(match: re.Match[str], *, keep_left: int, keep_right: int) -> str:
    """Mask the middle of a matched value, keeping its leading/trailing edges.

    PII must stay recognisable (so an auditor can still match it against a
    ledger) without exposing the full value — 138****8000 for a mobile, the
    standard first-6/last-4 form for an ID card.
    """
    value = match.group(0)
    stars = "*" * max(1, len(value) - keep_left - keep_right)
    return value[:keep_left] + stars + value[-keep_right:]


_DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "API_KEY",
        re.compile(
            r"(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36,})",
            re.IGNORECASE,
        ),
        lambda _: "[REDACTED]",
    ),
    RedactionRule(
        "CN_MOBILE",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        lambda m: _mask_keep_edges(m, keep_left=3, keep_right=4),
    ),
    RedactionRule(
        "CN_ID_CARD",
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        lambda m: _mask_keep_edges(m, keep_left=6, keep_right=4),
    ),
)


class Redactor:
    """Deterministic output scrubber over a configurable rule set."""

    def __init__(self, rules: Sequence[RedactionRule] | None = None) -> None:
        # Defaults when the caller does not pin a rule set (tests/ops can).
        self._rules = list(rules) if rules is not None else list(_DEFAULT_RULES)

    def redact(self, text: str) -> RedactionResult:
        """Return *text* with every matched secret/PII masked in place."""
        redacted = text
        count = 0
        matched_names: list[str] = []
        for rule in self._rules:
            redacted, n = rule.pattern.subn(rule.replace, redacted)
            if n:
                count += n
                matched_names.append(rule.name)
        return RedactionResult(
            redacted=redacted,
            count=count,
            matched_rules=tuple(matched_names),
        )
