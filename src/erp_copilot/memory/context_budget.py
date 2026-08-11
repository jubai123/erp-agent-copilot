"""Token budget and context compression — task 4.13.

docs/03 §8 reserves a per-category token budget for the LLM context (system
instruction, tool schema, plan, knowledge, history, output). When a section
overruns its cap — or the sum overruns the total — the budget compresses
tool results and history first; security policy, approval info and citation
references are never compressed.

No tokenizer dependency is approved in the stack, so counting defaults to a
deterministic heuristic (≈1 token per 4 characters) and callers may inject a
real tokenizer once one exists.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from enum import StrEnum


class BudgetCategory(StrEnum):
    """Context sections counted against the LLM budget (docs/03 §8)."""

    SYSTEM_INSTRUCTION = "system_instruction"
    TOOL_SCHEMA = "tool_schema"
    PLAN = "plan"
    KNOWLEDGE = "knowledge"
    HISTORY = "history"
    TOOL_RESULTS = "tool_results"
    OUTPUT = "output"
    APPROVALS = "approvals"
    CITATIONS = "citations"


# Sections that must never be compressed: the system instruction carries the
# security policy, approvals are an audit trail, and citation references keep
# knowledge grounded. Cutting them would hide a scope decision or break
# reproducibility (docs/03 §8).
PROTECTED_CATEGORIES: frozenset[BudgetCategory] = frozenset(
    {
        BudgetCategory.SYSTEM_INSTRUCTION,
        BudgetCategory.APPROVALS,
        BudgetCategory.CITATIONS,
    }
)

# Compression order: first = lowest retention, cut first. Tool results and
# history go before plan and knowledge; the protected sections above never
# appear here.
_COMPRESS_PRIORITY: tuple[BudgetCategory, ...] = (
    BudgetCategory.TOOL_RESULTS,
    BudgetCategory.HISTORY,
    BudgetCategory.KNOWLEDGE,
    BudgetCategory.PLAN,
    BudgetCategory.TOOL_SCHEMA,
    BudgetCategory.OUTPUT,
)


def estimate_tokens(text: str) -> int:
    """Approximate tokens as 1 per 4 characters (deterministic, no dependency)."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def truncate(
    text: str,
    max_tokens: int,
    token_counter: Callable[[str], int] = estimate_tokens,
    marker: str = "…[truncated]",
) -> str:
    """Cut *text* to fit *max_tokens*, keeping the head and a truncation marker.

    The marker's own tokens count against the cap so the result always fits.
    *token_counter* must be monotonic in string length for the
    longest-fitting-head search to be correct.
    """
    if token_counter(text) <= max_tokens:
        return text
    marker_tokens = token_counter(marker)
    if marker_tokens >= max_tokens:
        return marker

    def fits(chars: int) -> bool:
        return token_counter(text[:chars]) + marker_tokens <= max_tokens

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + marker


class ContextBudget:
    """Per-category token caps with priority-ordered compression.

    *caps* reserves a token budget per section; *total* caps the sum of all
    sections (None skips the sum check). *token_counter* injects a real
    tokenizer; the default is the deterministic heuristic.
    """

    def __init__(
        self,
        caps: Mapping[BudgetCategory, int],
        *,
        total: int | None = None,
        token_counter: Callable[[str], int] = estimate_tokens,
    ) -> None:
        self.caps = caps
        self.total = total
        self.token_counter = token_counter

    def total_tokens(self, sections: Mapping[BudgetCategory, str]) -> int:
        """Sum the token estimates across every section."""
        return sum(self.token_counter(text) for text in sections.values())

    def over_cap(self, category: BudgetCategory, text: str) -> bool:
        """True when *text* exceeds the section's reserved cap."""
        cap = self.caps.get(category)
        return cap is not None and self.token_counter(text) > cap

    def within_budget(self, sections: Mapping[BudgetCategory, str]) -> bool:
        """True when every section fits its cap and the total fits."""
        if self.total is not None and self.total_tokens(sections) > self.total:
            return False
        return not any(self.over_cap(category, text) for category, text in sections.items())

    def fit_section(self, category: BudgetCategory, text: str) -> str:
        """Return *text* under its cap; protected sections are left intact.

        A protected section over its cap is returned unchanged — the budget
        refuses to compress security policy, approval info or citations, so
        an over-cap protected section signals a misconfigured budget.
        """
        cap = self.caps.get(category)
        if cap is None or self.token_counter(text) <= cap:
            return text
        if category in PROTECTED_CATEGORIES:
            return text
        return truncate(text, cap, self.token_counter)

    def trim(self, sections: Mapping[BudgetCategory, str]) -> dict[BudgetCategory, str]:
        """Compress each section to its cap, tool results and history first.

        When the sum still overruns *total*, the lowest-retention sections are
        compressed further in priority order. Protected sections are never
        touched; if they alone overrun the budget, trim returns its best effort
        rather than deleting them.
        """
        fitted: dict[BudgetCategory, str] = dict(sections)
        for category in _COMPRESS_PRIORITY:
            if category in fitted:
                fitted[category] = self.fit_section(category, fitted[category])
        if self.total is None or self.total_tokens(fitted) <= self.total:
            return fitted

        caps = dict(self.caps)
        while self.total_tokens(fitted) > self.total:
            shrunk_any = False
            for category in _COMPRESS_PRIORITY:
                if category not in fitted:
                    continue
                old_cap = caps.get(category)
                if old_cap is None:
                    continue
                new_cap = max(1, old_cap // 2)
                if new_cap == old_cap:
                    continue
                caps[category] = new_cap
                fitted[category] = truncate(fitted[category], new_cap, self.token_counter)
                shrunk_any = True
                if self.total_tokens(fitted) <= self.total:
                    return fitted
            if not shrunk_any:
                return fitted
        return fitted
