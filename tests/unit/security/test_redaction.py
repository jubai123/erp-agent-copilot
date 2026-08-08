"""Unit tests for security/redaction.py — task 5.5.

Redaction is a pure output-layer transformer (docs/06 §5 output layer): it
deterministically replaces secret and PII patterns in text — API keys become
[REDACTED], Chinese mobile numbers and ID card numbers are partially masked.
Unlike the guards it is not an interception point: it rewrites output in place
and touches no database, so the result only carries the redacted text and a
replacement count. Tests run the same pattern as test_injection_guard.
"""

from __future__ import annotations

import re

import pytest

from erp_copilot.security.redaction import RedactionRule, Redactor


class TestAPIKey:
    """Known key prefixes are replaced wholesale with [REDACTED]."""

    def test_openai_style_key_replaced(self) -> None:
        result = Redactor().redact("使用密钥 sk-abc123DEF456ghi789 调用接口")
        assert result.redacted == "使用密钥 [REDACTED] 调用接口"
        assert result.count == 1
        assert "API_KEY" in result.matched_rules

    def test_aws_access_key_replaced(self) -> None:
        result = Redactor().redact("aws key AKIAIOSFODNN7EXAMPLE")
        assert result.redacted == "aws key [REDACTED]"
        assert result.count == 1

    def test_multiple_keys_all_replaced(self) -> None:
        result = Redactor().redact("sk-keyOneABC123def456 sk-keyTwoGHI789jkl012")
        assert result.redacted == "[REDACTED] [REDACTED]"
        assert result.count == 2


class TestCNMobile:
    """11-digit Chinese mobile numbers keep first 3 and last 4 digits."""

    def test_bare_mobile_masked(self) -> None:
        result = Redactor().redact("13800138000")
        assert result.redacted == "138****8000"
        assert result.count == 1
        assert "CN_MOBILE" in result.matched_rules

    def test_mobile_inside_sentence(self) -> None:
        result = Redactor().redact("联系电话 13912345678 请尽快联系")
        assert result.redacted == "联系电话 139****5678 请尽快联系"

    def test_multiple_mobiles_masked(self) -> None:
        result = Redactor().redact("13800138000 15912345678")
        assert result.redacted == "138****8000 159****5678"
        assert result.count == 2


class TestCNIdCard:
    """18-digit Chinese ID card numbers keep first 6 and last 4 digits."""

    def test_id_card_masked(self) -> None:
        result = Redactor().redact("身份证号 11010119900307123X")
        assert result.redacted == "身份证号 110101********123X"
        assert result.count == 1
        assert "CN_ID_CARD" in result.matched_rules

    def test_digits_only_id_card_masked(self) -> None:
        result = Redactor().redact("110101199003071234")
        assert result.redacted == "110101********1234"


class TestNoFalsePositives:
    """Realistic ERP business data must pass through untouched."""

    @pytest.mark.parametrize(
        "text",
        [
            "客户：张伟；商品：苹果；数量：5；金额：1500.00 元；备注：明日发货",
            "订单号 NO-20260809-001，发货地址 上海市浦东新区",
            "客服电话：010-12345678（座机，非手机，不隐藏）",
            "SKU ABC-12345 库存 12345 件",
            "编号 12345678901234567",  # 17 digits — not an 18-char ID
            "收款人 李四，微信号 wxid_abc123",
        ],
    )
    def test_business_text_unchanged(self, text: str) -> None:
        result = Redactor().redact(text)
        assert result.redacted == text
        assert result.count == 0
        assert result.matched_rules == ()

    def test_clean_text_result_shape(self) -> None:
        result = Redactor().redact("查询苹果的库存")
        assert result.redacted == "查询苹果的库存"
        assert result.count == 0
        assert result.matched_rules == ()


class TestCustomRules:
    def test_custom_rules_replace_defaults(self) -> None:
        rule = RedactionRule("CUSTOM", re.compile(r"hacker"), lambda _: "***")
        redactor = Redactor(rules=[rule])
        assert redactor.redact("the hacker strikes").redacted == "the *** strikes"
        assert redactor.redact("sk-realKey").redacted == "sk-realKey"  # defaults gone

    def test_no_rules_passthrough(self) -> None:
        redactor = Redactor(rules=[])
        assert redactor.redact("13800138000").redacted == "13800138000"
        assert redactor.redact("13800138000").count == 0
