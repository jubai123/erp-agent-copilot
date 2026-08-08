"""Prompt-injection guard — task 5.4.

A pure, deterministic pre-LLM check (docs/06 §7): scans user input or
retrieved knowledge text against a small set of known injection phrases using
regular expressions. It returns an :class:`InjectionVerdict` instead of
raising; the caller decides the disposition — block at the input layer,
quarantine at the knowledge layer (RAG poisoning). Regular expressions keep the
behaviour deterministic and auditable, consistent with the project's
determinism-first security stance (docs/06 §1). Flagged events are persisted to
security_events by :func:`record_injection_event`; the guard itself stays pure.

This is a narrow, well-understood detector, not a blanket filter: it recognises
the classic "override your instructions / skip approvals / leak your system
prompt" families, which is what task 5.4's acceptance criteria require. It
deliberately does not attempt general malicious-intent classification.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from erp_copilot.domain.entities import SecurityEvent


@dataclass(frozen=True)
class InjectionRule:
    """One injection phrase family: a stable name and a compiled pattern."""

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class InjectionVerdict:
    """Outcome of a guard check; callers decide the disposition.

    matched_rules holds every family that hit, in rule order, so a caller can
    see all reasons. detail is the primary (first) rule name.
    """

    flagged: bool
    matched_rules: tuple[str, ...] = ()
    detail: str | None = None


_DEFAULT_RULES: tuple[InjectionRule, ...] = (
    InjectionRule(
        "IGNORE_PRIOR_INSTRUCTIONS",
        re.compile(
            r"忽略(?:之前|以上所有|以上|所有之前)的?指令"
            r"|ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"
            r"|ignore\s+the\s+instructions\s+above",
            re.IGNORECASE,
        ),
    ),
    InjectionRule(
        "BYPASS_APPROVAL",
        re.compile(
            r"跳过审批|绕过审批|绕过审核|绕过审批流程"
            r"|bypass\s+(?:the\s+)?approval"
            r"|skip\s+(?:the\s+)?approval",
            re.IGNORECASE,
        ),
    ),
    InjectionRule(
        "LEAK_SYSTEM_PROMPT",
        re.compile(
            r"(?:泄露|泄漏|显示|出示|展示)(?:你的|你们的)?系统提示词"
            r"|(?:reveal|show|print|display)\s+(?:your\s+)?system\s+prompt",
            re.IGNORECASE,
        ),
    ),
)


class InjectionGuard:
    """Pure regex-based detector over a configurable rule set."""

    def __init__(self, rules: Sequence[InjectionRule] | None = None) -> None:
        # Defaults when the caller does not pin a rule set (tests/ops can).
        self._rules = list(rules) if rules is not None else list(_DEFAULT_RULES)

    def check(self, text: str) -> InjectionVerdict:
        """Flag *text* when any rule's pattern matches; pure, no I/O."""
        matched = tuple(rule.name for rule in self._rules if rule.pattern.search(text))
        if not matched:
            return InjectionVerdict(flagged=False)
        return InjectionVerdict(flagged=True, matched_rules=matched, detail=matched[0])


def record_injection_event(
    session: Session,
    *,
    run_id: str | None,
    text: str,
    verdict: InjectionVerdict,
    disposition: str = "blocked",
) -> SecurityEvent:
    """Persist one injection interception into security_events (docs/07).

    Called by the executor when the guard returns a flagged verdict; the caller
    chooses disposition — "blocked" at the input layer, "quarantined" for RAG
    knowledge that fails the check.
    """
    event = SecurityEvent(
        attack_type="PROMPT_INJECTION",
        layer="injection_guard",
        severity="HIGH",
        input_summary=text,
        disposition=disposition,
        run_id=run_id,
    )
    session.add(event)
    session.commit()
    return event
