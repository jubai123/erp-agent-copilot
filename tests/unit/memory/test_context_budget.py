"""Unit tests for memory/context_budget.py — task 4.13.

The context budget reserves a per-category token cap for the LLM context and
orders compression so tool results and history are cut first; security
policy, approval info and citation references are never compressed (docs/03
§8). Tests inject an exact token counter (one character == one token) so the
arithmetic is deterministic, plus a few checks on the default heuristic.
"""

from __future__ import annotations

from erp_copilot.memory.context_budget import (
    PROTECTED_CATEGORIES,
    BudgetCategory,
    ContextBudget,
    estimate_tokens,
    truncate,
)

_MARKER = "…[truncated]"


def _chars(text: str) -> int:
    """Exact token counter: one character counts as one token."""
    return len(text)


class TestEstimateTokens:
    def test_empty_text_counts_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_short_text_counts_at_least_one(self) -> None:
        assert estimate_tokens("a") == 1
        assert estimate_tokens("abc") == 1

    def test_roughly_one_token_per_four_chars(self) -> None:
        assert estimate_tokens("abcdefgh") == 2
        assert estimate_tokens("x" * 40) == 10


class TestTruncate:
    def test_short_text_returned_unchanged(self) -> None:
        assert truncate("short", 10, _chars) == "short"

    def test_long_text_cut_to_cap_with_marker(self) -> None:
        result = truncate("x" * 100, 20, _chars)
        assert _chars(result) <= 20
        assert result.startswith("x")
        assert result.endswith(_MARKER)
        assert len(result) < 100

    def test_marker_tokens_count_against_budget(self) -> None:
        result = truncate("x" * 100, 15, _chars)
        # head + marker must fit the cap exactly
        assert _chars(result) == 15

    def test_cap_smaller_than_marker_returns_marker(self) -> None:
        assert truncate("x" * 10, 1, _chars) == _MARKER

    def test_default_counter_when_not_injected(self) -> None:
        # 40 chars ≈ 10 tokens, well under the cap → unchanged.
        assert truncate("x" * 40, 50) == "x" * 40
        result = truncate("x" * 1000, 50)
        assert estimate_tokens(result) <= 50


class TestContextBudget:
    def test_within_budget_when_every_section_fits(self) -> None:
        budget = ContextBudget(
            caps={BudgetCategory.TOOL_RESULTS: 100, BudgetCategory.HISTORY: 100},
            total=200,
            token_counter=_chars,
        )
        assert budget.within_budget(
            {
                BudgetCategory.TOOL_RESULTS: "x" * 60,
                BudgetCategory.HISTORY: "y" * 40,
            }
        )

    def test_over_single_category_fails_budget(self) -> None:
        budget = ContextBudget(caps={BudgetCategory.TOOL_RESULTS: 10}, token_counter=_chars)
        assert not budget.within_budget({BudgetCategory.TOOL_RESULTS: "x" * 20})

    def test_sum_over_total_fails_budget(self) -> None:
        budget = ContextBudget(
            caps={BudgetCategory.TOOL_RESULTS: 100, BudgetCategory.HISTORY: 100},
            total=150,
            token_counter=_chars,
        )
        assert not budget.within_budget(
            {
                BudgetCategory.TOOL_RESULTS: "x" * 100,
                BudgetCategory.HISTORY: "y" * 100,
            }
        )

    def test_no_total_ignores_the_sum(self) -> None:
        budget = ContextBudget(
            caps={BudgetCategory.TOOL_RESULTS: 100, BudgetCategory.HISTORY: 100},
            total=None,
            token_counter=_chars,
        )
        # Each section fits its own cap; without a total the sum is not checked.
        assert budget.within_budget(
            {
                BudgetCategory.TOOL_RESULTS: "x" * 100,
                BudgetCategory.HISTORY: "y" * 100,
            }
        )


class TestProtectedCategories:
    def test_security_approval_citation_are_protected(self) -> None:
        assert {
            BudgetCategory.SYSTEM_INSTRUCTION,
            BudgetCategory.APPROVALS,
            BudgetCategory.CITATIONS,
        } <= PROTECTED_CATEGORIES

    def test_fit_section_never_compresses_protected(self) -> None:
        budget = ContextBudget(caps={BudgetCategory.APPROVALS: 5}, token_counter=_chars)
        approvals = "approve record" * 4
        assert budget.over_cap(BudgetCategory.APPROVALS, approvals)
        assert budget.fit_section(BudgetCategory.APPROVALS, approvals) == approvals


class TestFitSection:
    def test_under_cap_returned_unchanged(self) -> None:
        budget = ContextBudget(caps={BudgetCategory.PLAN: 20}, token_counter=_chars)
        plan = "p" * 10
        assert budget.fit_section(BudgetCategory.PLAN, plan) == plan

    def test_compressible_over_cap_truncated(self) -> None:
        budget = ContextBudget(caps={BudgetCategory.TOOL_RESULTS: 20}, token_counter=_chars)
        results = "r" * 100
        fitted = budget.fit_section(BudgetCategory.TOOL_RESULTS, results)
        assert _chars(fitted) <= 20
        assert fitted != results

    def test_missing_cap_leaves_text_alone(self) -> None:
        budget = ContextBudget(caps={}, token_counter=_chars)
        assert budget.fit_section(BudgetCategory.KNOWLEDGE, "anything") == "anything"


class TestTrim:
    def test_all_within_budget_returns_identical_sections(self) -> None:
        budget = ContextBudget(
            caps={BudgetCategory.TOOL_RESULTS: 100, BudgetCategory.HISTORY: 100},
            total=200,
            token_counter=_chars,
        )
        sections = {
            BudgetCategory.TOOL_RESULTS: "x" * 60,
            BudgetCategory.HISTORY: "y" * 40,
        }
        assert budget.trim(sections) == sections

    def test_over_cap_section_compressed_others_intact(self) -> None:
        budget = ContextBudget(
            caps={BudgetCategory.TOOL_RESULTS: 20, BudgetCategory.HISTORY: 100},
            token_counter=_chars,
        )
        history = "y" * 100
        fitted = budget.trim(
            {
                BudgetCategory.TOOL_RESULTS: "x" * 100,
                BudgetCategory.HISTORY: history,
            }
        )
        assert _chars(fitted[BudgetCategory.TOOL_RESULTS]) <= 20
        assert fitted[BudgetCategory.HISTORY] == history

    def test_tool_results_compressed_first_when_total_over(self) -> None:
        # Compressing tool results alone brings the sum under the total, so
        # history and output must come through untouched.
        budget = ContextBudget(
            caps={
                BudgetCategory.TOOL_RESULTS: 100,
                BudgetCategory.HISTORY: 100,
                BudgetCategory.OUTPUT: 50,
            },
            total=220,
            token_counter=_chars,
        )
        sections = {
            BudgetCategory.TOOL_RESULTS: "x" * 100,
            BudgetCategory.HISTORY: "y" * 100,
            BudgetCategory.OUTPUT: "z" * 50,
        }
        fitted = budget.trim(sections)
        assert budget.total_tokens(fitted) <= 220
        assert _chars(fitted[BudgetCategory.TOOL_RESULTS]) < 100
        assert fitted[BudgetCategory.HISTORY] == "y" * 100
        assert fitted[BudgetCategory.OUTPUT] == "z" * 50

    def test_history_compressed_before_output(self) -> None:
        # A tighter total forces both tool results and history down while the
        # output — the lowest compression priority — stays intact.
        budget = ContextBudget(
            caps={
                BudgetCategory.TOOL_RESULTS: 100,
                BudgetCategory.HISTORY: 100,
                BudgetCategory.OUTPUT: 50,
            },
            total=150,
            token_counter=_chars,
        )
        sections = {
            BudgetCategory.TOOL_RESULTS: "x" * 100,
            BudgetCategory.HISTORY: "y" * 100,
            BudgetCategory.OUTPUT: "z" * 50,
        }
        fitted = budget.trim(sections)
        assert budget.total_tokens(fitted) <= 150
        assert _chars(fitted[BudgetCategory.TOOL_RESULTS]) < 100
        assert _chars(fitted[BudgetCategory.HISTORY]) < 100
        assert fitted[BudgetCategory.OUTPUT] == "z" * 50

    def test_protected_sections_survive_trim(self) -> None:
        # Protected sections stay over their own caps yet are never touched;
        # the compressible section absorbs the whole cut.
        budget = ContextBudget(
            caps={
                BudgetCategory.SYSTEM_INSTRUCTION: 10,
                BudgetCategory.APPROVALS: 10,
                BudgetCategory.TOOL_RESULTS: 100,
            },
            total=200,
            token_counter=_chars,
        )
        policy = "policy" * 10
        approvals = "approval" * 10
        fitted = budget.trim(
            {
                BudgetCategory.SYSTEM_INSTRUCTION: policy,
                BudgetCategory.APPROVALS: approvals,
                BudgetCategory.TOOL_RESULTS: "x" * 100,
            }
        )
        assert fitted[BudgetCategory.SYSTEM_INSTRUCTION] == policy
        assert fitted[BudgetCategory.APPROVALS] == approvals
        assert _chars(fitted[BudgetCategory.TOOL_RESULTS]) < 100
