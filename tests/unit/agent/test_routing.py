"""Unit tests for the three-layer funnel router — route_query_layer.

The router decides which funnel layer serves a query: tier1 (deterministic
planner), tier2 (LLM constrained planning) or tier3 (low-confidence free
planning). It is the production decision the routing eval consumes, so the
dataset oracle — 50 cases in evals/datasets/agent_routing_50.json — must be
reproduced exactly: every case's expected_layer equals route_query_layer's
answer.

The overgrab hazard this pins: the deterministic chain produces a plausible
but wrong plan for queries beyond its grammar (comparisons, conditions,
multi-intent, fuzzy asks, out-of-vocab regions). The router must throw those
up instead of letting tier1 grab them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_copilot.agent.routing import _complexity, _unknown_region, route_query_layer

_DATASET = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "agent_routing_50.json"
_LAYERS = {"tier1", "tier2", "tier3"}


def _cases() -> list[dict]:
    return json.loads(_DATASET.read_text(encoding="utf-8"))["cases"]


def _tier1_queries() -> list[str]:
    return [c["query"] for c in _cases() if c["expected_layer"] == "tier1"]


class TestDatasetOracle:
    """The router must reproduce the dataset's expected layer for all 50 cases."""

    @pytest.mark.parametrize(
        "case",
        _cases(),
        ids=[c["case_id"] for c in _cases()],
    )
    def test_router_matches_expected_layer(self, case: dict) -> None:
        assert route_query_layer(case["query"]) == case["expected_layer"], case["case_id"]


class TestTier1Precision:
    """No tier1 query may trip a complexity trigger — false positives regress the 20 hits."""

    @pytest.mark.parametrize("query", _tier1_queries())
    def test_tier1_query_is_not_complex(self, query: str) -> None:
        assert _complexity(query) is None, query

    def test_all_twenty_tier1_cases_stay_tier1(self) -> None:
        assert sum(c["expected_layer"] == "tier1" for c in _cases()) == 20
        for case in _cases():
            if case["expected_layer"] == "tier1":
                assert route_query_layer(case["query"]) == "tier1", case["case_id"]


class TestComplexityGates:
    """Each beyond-grammar class must route to tier3 regardless of what tier1 would grab."""

    @pytest.mark.parametrize(
        "query",
        [
            "苹果和香蕉哪个贵",  # comparison: 哪个贵
            "对比电脑和键盘的性价比",  # comparison: 对比 / 性价比
            "键盘比鼠标贵多少",  # comparison: 比…贵
            "如果键盘缺货就换成鼠标",  # condition: 如果
            "苹果卖完了吗，卖完了就看看橙子",  # condition: 卖完
            "先取消订单 3f2a9c1d 再下一单香蕉",  # multi-intent: 先…再
            "苹果和键盘都要多少钱",  # multi-entity: 2 products
            "对比一下上海和南京的供应商",  # multi-entity: 2 regions
            "键盘的供应商是哪家",  # association: 的供应商
            "香蕉的供应商是谁",  # association: 供应商是谁
            "帮我推荐一个性价比高的键盘",  # fuzzy: 性价比 / 推荐一
            "看看电脑能不能分期付款",  # fuzzy: 能不能 / 分期
            "明天能到货的订单有哪些",  # time: 明天
            "帮我安排一下本周的补货计划",  # fuzzy/time: 计划 / 本周
        ],
    )
    def test_beyond_grammar_routes_to_tier3(self, query: str) -> None:
        assert route_query_layer(query) == "tier3"


class TestUnknownRegion:
    """An uncovered city in a supplier query can't be scoped — route up to tier2."""

    def test_supplier_query_with_uncovered_city_routes_up(self) -> None:
        assert route_query_layer("苏州有哪些供应商") == "tier2"
        assert route_query_layer("武汉有配送吗") == "tier2"

    def test_uncovered_city_only_counts_for_supplier_intent(self) -> None:
        from erp_copilot.agent.nodes.classify_intent import classify_intent

        order_intent = classify_intent("杭州下单买苹果")  # order/create, OOV region
        assert _unknown_region("杭州下单买苹果", order_intent) is False

    def test_recognized_regions_do_not_route_up(self) -> None:
        assert route_query_layer("上海有哪些供应商") == "tier1"
        assert route_query_layer("南京有供应商吗") == "tier1"


class TestBoundaries:
    def test_honest_empty_plan_defaults_to_tier2(self) -> None:
        assert route_query_layer("榴莲多少钱") == "tier2"  # OOV product -> EMPTY_PLAN
        assert route_query_layer("创建一笔订单购买苹果") == "tier2"  # missing region

    def test_complex_query_with_empty_plan_goes_to_tier3(self) -> None:
        assert route_query_layer("明天能到货的订单有哪些") == "tier3"

    def test_known_single_step_tier1(self) -> None:
        assert route_query_layer("苹果多少钱") == "tier1"
        assert route_query_layer("取消订单 3f2a9c1d") == "tier1"
