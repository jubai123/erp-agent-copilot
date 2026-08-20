"""Unit tests for agent/nodes/classify_intent.py — task 4.3.

The node is the graph's first step: it maps a natural-language query to a
deterministic (domain, action) intent, an initial risk level, and explicit
entities (docs/03 §4).  Deterministic by the ADR rule "确定性优先，概率兜底" —
the (domain, action) output feeds two downstream deterministic layers
(rule_matcher L1 injection and candidate_filter tool filtering) that require
exact key hits.  Unrecognized queries fall back to the lowest-risk read-only
intent instead of raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from erp_copilot.agent.nodes.classify_intent import classify_intent, classify_intent_node
from erp_copilot.agent.state import AgentState, AgentStatus
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.vocabulary import loader


class TestIntentMapping:
    def test_product_query(self) -> None:
        intent = classify_intent("苹果多少钱一斤")
        assert (intent.domain, intent.action) == ("product", "query")
        assert intent.risk_level == ToolRiskLevel.READ

    def test_product_check_stock(self) -> None:
        intent = classify_intent("查苹果库存")
        assert (intent.domain, intent.action) == ("product", "check_stock")
        assert intent.risk_level == ToolRiskLevel.READ

    def test_supplier_query(self) -> None:
        intent = classify_intent("上海有哪些供应商")
        assert (intent.domain, intent.action) == ("supplier", "query")
        assert intent.risk_level == ToolRiskLevel.READ

    def test_order_query(self) -> None:
        intent = classify_intent("查询订单 abc123def456 的状态")
        assert (intent.domain, intent.action) == ("order", "query")
        assert intent.risk_level == ToolRiskLevel.READ

    def test_order_create(self) -> None:
        intent = classify_intent("下单购买10KG苹果发往上海")
        assert (intent.domain, intent.action) == ("order", "create")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_order_cancel(self) -> None:
        intent = classify_intent("取消订单 abc123def456")
        assert (intent.domain, intent.action) == ("order", "cancel")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_order_update_status(self) -> None:
        intent = classify_intent("确认订单 abc123def456")
        assert (intent.domain, intent.action) == ("order", "update_status")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_order_modify(self) -> None:
        intent = classify_intent("修改订单 abc123def456 的数量")
        assert (intent.domain, intent.action) == ("order", "modify")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_security_data_access(self) -> None:
        intent = classify_intent("查询数据访问权限")
        assert (intent.domain, intent.action) == ("security", "data_access")
        assert intent.risk_level == ToolRiskLevel.DANGEROUS

    def test_system_scenario(self) -> None:
        intent = classify_intent("切换到库存不足场景")
        assert (intent.domain, intent.action) == ("system", "scenario")


class TestFallback:
    def test_unknown_query_defaults_to_lowest_risk_read(self) -> None:
        intent = classify_intent("今天天气怎么样")
        assert (intent.domain, intent.action) == ("product", "query")
        assert intent.risk_level == ToolRiskLevel.READ

    def test_unknown_query_produces_empty_entities(self) -> None:
        assert classify_intent("你好").entities == {}


class TestPrecedence:
    def test_order_cancel_beats_product_intent(self) -> None:
        # 苹果 (product) and 采购 (create) both present — cancel must win.
        intent = classify_intent("取消苹果的采购订单")
        assert (intent.domain, intent.action) == ("order", "cancel")

    def test_system_scenario_beats_stock(self) -> None:
        intent = classify_intent("切换到库存不足场景")
        assert (intent.domain, intent.action) == ("system", "scenario")


class TestEntityExtraction:
    def test_product_name(self) -> None:
        assert classify_intent("查苹果库存").entities["product"] == "苹果"

    def test_region(self) -> None:
        assert classify_intent("上海有哪些供应商").entities["region"] == "上海"

    def test_quantity_with_unit(self) -> None:
        entities = classify_intent("下单购买10KG苹果发往上海").entities
        assert entities["quantity"] == 10
        assert entities["unit"] == "KG"

    def test_order_id(self) -> None:
        assert (
            classify_intent("查询订单 abc123def456 的状态").entities["order_id"] == "abc123def456"
        )

    def test_digits_inside_order_id_are_not_quantity(self) -> None:
        # 123 is embedded in the 12-hex order id — must not leak as quantity.
        entities = classify_intent("查询订单 abc123def456 的状态").entities
        assert "quantity" not in entities

    def test_no_entities_when_none_present(self) -> None:
        assert classify_intent("查一下库存情况").entities == {}


class TestClassifyIntentNode:
    def test_sets_intent_and_planning_status(self) -> None:
        state = AgentState(run_id="r1", tenant_id="t1", query="查苹果库存")
        result = classify_intent_node(state)
        assert result["status"] == AgentStatus.PLANNING
        intent = result["intent"]
        assert (intent.domain, intent.action) == ("product", "check_stock")

    def test_unknown_query_does_not_raise(self) -> None:
        state = AgentState(run_id="r2", tenant_id="t1", query="随便聊聊")
        result = classify_intent_node(state)
        assert result["intent"].domain == "product"


@pytest.fixture(autouse=True)
def _reset_vocab_cache():
    """classify_intent reads the process-global vocabulary cache; leave it clean."""
    yield
    loader.invalidate()


def _raw_manifest() -> dict:
    with open(loader._MANIFEST_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestCatalogDrivenEntities:
    """Entity extraction reads the merged catalog, not hardcoded tuples."""

    def test_oov_product_extracts_no_product_entity(self) -> None:
        assert "product" not in classify_intent("榴莲多少钱").entities

    def test_oov_region_extracts_no_region_entity(self) -> None:
        assert "region" not in classify_intent("苏州有哪些供应商").entities

    def test_new_catalog_product_is_extracted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _raw_manifest()
        raw["products"].append({"name": "榴莲"})
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(loader, "_MANIFEST_PATH", manifest)
        loader.invalidate()
        assert classify_intent("榴莲多少钱").entities["product"] == "榴莲"

    def test_new_catalog_unit_updates_quantity_parser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _raw_manifest()
        raw["product_unit_enum"].append("箱")
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(loader, "_MANIFEST_PATH", manifest)
        loader.invalidate()
        entities = classify_intent("下单购买3箱苹果").entities
        assert entities["quantity"] == 3
        assert entities["unit"] == "箱"
