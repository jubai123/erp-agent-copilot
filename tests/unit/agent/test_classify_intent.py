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

    def test_query_by_status_beats_update_status(self) -> None:
        # 已发货的订单 is a read; only bare 已发货 is the write verb.
        intent = classify_intent("已发货的订单有哪些")
        assert (intent.domain, intent.action) == ("order", "query_by_status")

    def test_query_by_supplier_beats_supplier_query(self) -> None:
        intent = classify_intent("供应商的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_supplier")

    def test_supplier_create_beats_supplier_query(self) -> None:
        intent = classify_intent("添加供应商 旧物流")
        assert (intent.domain, intent.action) == ("supplier", "create")

    def test_product_update_beats_order_update_status(self) -> None:
        # The bare "改为" verb must not swallow a product substitute update.
        intent = classify_intent("把商品 4 号的替代品改为西瓜")
        assert (intent.domain, intent.action) == ("product", "update")

    def test_order_update_status_unaffected_by_product_rules(self) -> None:
        intent = classify_intent("把订单 a1b2c3 改为已发货")
        assert (intent.domain, intent.action) == ("order", "update_status")

    def test_order_create_beats_supplier_mention(self) -> None:
        # A create order that names a supplier must not route to supplier/query.
        intent = classify_intent("找一个可用的供应商，在北京买 8 台电脑")
        assert (intent.domain, intent.action) == ("order", "create")


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

    def test_supplier_id(self) -> None:
        assert classify_intent("id为3的物流供应商有哪些").entities["supplier_id"] == 3

    def test_supplier_name(self) -> None:
        assert classify_intent("添加供应商 旧物流").entities["supplier_name"] == "旧物流"

    def test_name_after_for_create(self) -> None:
        entities = classify_intent("添加名称为西瓜的商品").entities
        assert entities["name"] == "西瓜"

    def test_price_and_stock(self) -> None:
        entities = classify_intent("添加名称为西瓜的商品 价格10元 库存100").entities
        assert entities["price"] == 10.0
        assert entities["stock"] == 100

    def test_description(self) -> None:
        entities = classify_intent("修改商品 3 号描述为鲜甜多汁").entities
        assert entities["description"] == "鲜甜多汁"

    def test_substitute_name(self) -> None:
        entities = classify_intent("把商品 4 号的替代品改为西瓜").entities
        assert entities["substitute_name"] == "西瓜"

    def test_all_regions(self) -> None:
        entities = classify_intent("添加供应商 旧物流，覆盖上海和北京").entities
        assert entities["regions"] == ["上海", "北京"]
        assert entities["region"] == "上海"

    def test_date_year_is_not_quantity(self) -> None:
        entities = classify_intent("查询2023年1月的订单").entities
        assert "quantity" not in entities

    def test_supplier_id_digits_are_not_quantity(self) -> None:
        entities = classify_intent("id为3的物流供应商有哪些").entities
        assert "quantity" not in entities


class TestM0OrderReadIntents:
    """V5-aligned order query sub-intents (M0)."""

    def test_query_by_status(self) -> None:
        intent = classify_intent("已发货的订单有哪些")
        assert (intent.domain, intent.action) == ("order", "query_by_status")
        assert intent.entities["status"] == "SHIPPED"

    def test_query_by_status_pending(self) -> None:
        intent = classify_intent("待确认的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_status")
        assert intent.entities["status"] == "CONFIRMED"

    def test_query_by_time_keyword(self) -> None:
        intent = classify_intent("本月的订单有哪些")
        assert (intent.domain, intent.action) == ("order", "query_by_time")

    def test_query_by_time_date_range(self) -> None:
        intent = classify_intent("查询2023年1月1日至2023年1月31日之间的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_time")
        assert intent.entities["time_range"] == ("2023-01-01", "2023-01-31")

    def test_query_by_time_lone_month_expands(self) -> None:
        intent = classify_intent("查询2023年1月的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_time")
        assert intent.entities["time_range"] == ("2023-01-01", "2023-01-31")

    def test_query_by_product_keyword(self) -> None:
        intent = classify_intent("该商品的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_product")

    def test_query_by_product_with_id(self) -> None:
        intent = classify_intent("商品 3 号的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_product")
        assert intent.entities["product_id"] == 3

    def test_query_by_supplier_with_id(self) -> None:
        intent = classify_intent("供应商 3 号的订单")
        assert (intent.domain, intent.action) == ("order", "query_by_supplier")
        assert intent.entities["supplier_id"] == 3


class TestM0WriteIntents:
    """V5-aligned supplier/product maintenance intents (M0)."""

    def test_supplier_create(self) -> None:
        intent = classify_intent("添加供应商 旧物流")
        assert (intent.domain, intent.action) == ("supplier", "create")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_supplier_delete(self) -> None:
        intent = classify_intent("删除供应商 旧物流")
        assert (intent.domain, intent.action) == ("supplier", "delete")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_product_add(self) -> None:
        intent = classify_intent("添加名称为西瓜的商品")
        assert (intent.domain, intent.action) == ("product", "add")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_product_update(self) -> None:
        intent = classify_intent("修改商品 3 号描述为鲜甜多汁")
        assert (intent.domain, intent.action) == ("product", "update")
        assert intent.risk_level == ToolRiskLevel.WRITE

    def test_product_delete(self) -> None:
        intent = classify_intent("删除商品 4 号")
        assert (intent.domain, intent.action) == ("product", "delete")
        assert intent.risk_level == ToolRiskLevel.WRITE


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
