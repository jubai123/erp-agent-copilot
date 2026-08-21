"""Unit tests for evals/agent_routing_eval.py — the funnel router's boundary.

The eval answers "query 应落三层漏斗的哪一层" by observing the production
router (route_query_layer, erp_copilot.agent.routing) over agent_routing_50.json.
observe_layer maps its answer to the 2×2 vocabulary ("tier1" / "route_up");
observe_layer_exact keeps the precise tier2/tier3 assignment. The 2×2 matrix
scores the boundary honestly: tier1-expected queries must be served with the
exact expected tool sequence; tier2/3-expected queries must route up — a tier1
grab there is an overgrab and FAILs so the funnel's honesty is measurable.

The router is purely deterministic (no LLM, no DB), so these tests run it over
the full 50-case dataset and pin the measured boundary. The router reproduces
the authored oracle exactly: 30 route-up queries (15 tier2 + 15 tier3) are all
thrown up, none over-grabbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_copilot.tools.candidate_filter import V6_TOOL_NAMES
from evals.agent_routing_eval import (
    evaluate_cases,
    observe_layer,
    observe_layer_exact,
    observe_tools,
    score_case,
)

_DATASET = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "agent_routing_50.json"
_LAYERS = {"tier1", "tier2", "tier3"}


def _cases() -> list[dict]:
    return json.loads(_DATASET.read_text(encoding="utf-8"))["cases"]


class TestDataset:
    def test_50_cases_across_three_layers(self) -> None:
        cases = _cases()
        assert len(cases) == 50
        counts = {layer: sum(1 for c in cases if c["expected_layer"] == layer) for layer in _LAYERS}
        assert counts == {"tier1": 20, "tier2": 15, "tier3": 15}

    def test_every_case_has_valid_layer_and_unique_id(self) -> None:
        ids = [c["case_id"] for c in _cases()]
        assert len(ids) == len(set(ids))
        for case in _cases():
            assert case["expected_layer"] in _LAYERS, case["case_id"]
            assert case["query"], case["case_id"]
            assert case["note"], case["case_id"]

    def test_tier1_cases_carry_grounded_expected_tools(self) -> None:
        for case in _cases():
            if case["expected_layer"] != "tier1":
                assert "expected_tools" not in case, case["case_id"]
                continue
            tools = case["expected_tools"]
            assert tools, f"{case['case_id']}: tier1 case missing expected_tools"
            assert set(tools) <= V6_TOOL_NAMES, case["case_id"]

    def test_served_tier1_tools_are_subset_of_registry(self) -> None:
        """Tier1 cases may not name unregistered tools; registry may hold more.

        Relaxed from == : agent_routing_50 still serves the 9-tool baseline while
        V6_TOOL_NAMES grew to the full 25-tool V5 surface. M5 (optional) restores
        per-registered-tool tier1 coverage by adding routing cases.
        """
        served = {
            t for c in _cases() if c["expected_layer"] == "tier1" for t in c["expected_tools"]
        }
        assert served
        assert served <= set(V6_TOOL_NAMES)


class TestObserveLayer:
    def test_known_tier1_queries_are_served(self) -> None:
        assert observe_layer("苹果多少钱") == "tier1"
        assert observe_layer("上海有哪些供应商") == "tier1"
        assert observe_layer("在上海下单买苹果") == "tier1"

    def test_known_route_up_queries_throw_up(self) -> None:
        assert observe_layer("榴莲多少钱") == "route_up"
        assert observe_layer("帮我安排一下本周的补货计划") == "route_up"

    def test_oov_region_is_routed_up_by_the_router(self) -> None:
        # 苏州 is not a classifier region — the router throws the supplier query
        # up to tier2 instead of letting tier1 grab getSupplierByStatus. observe_tools
        # still shows what tier1 *would* have grabbed (the would-grab signal).
        assert observe_layer("苏州有哪些供应商") == "route_up"
        assert observe_layer_exact("苏州有哪些供应商") == "tier2"
        assert observe_tools("苏州有哪些供应商") == ["getSupplierByStatus"]

    def test_tier1_tool_sequence_matches_expected(self) -> None:
        assert observe_tools("苹果多少钱") == ["getProductByName"]
        assert observe_tools("取消订单 3f2a9c1d") == ["getOrderByOrderId", "cancelOrder"]
        assert observe_tools("在上海下单买苹果") == [
            "getProductByName",
            "querySuppliersByDeliveryRegion",
            "createOrder",
        ]


class TestScoreCase:
    def test_tier1_hit_with_exact_tools_passes(self) -> None:
        result = score_case(
            {
                "case_id": "x",
                "query": "苹果多少钱",
                "expected_layer": "tier1",
                "expected_tools": ["getProductByName"],
            }
        )
        assert result["passed"] is True
        assert result["detail"].startswith("tier1 命中")

    def test_tier1_hit_with_wrong_tools_fails(self) -> None:
        result = score_case(
            {
                "case_id": "x",
                "query": "苹果多少钱",
                "expected_layer": "tier1",
                "expected_tools": ["getProductById"],
            }
        )
        assert result["passed"] is False
        assert result["detail"].startswith("tier1 命中但工具序列错误")

    def test_tier1_expected_but_routed_up_is_coverage_gap(self) -> None:
        result = score_case(
            {
                "case_id": "x",
                "query": "榴莲多少钱",
                "expected_layer": "tier1",
                "expected_tools": ["getProductByName"],
            }
        )
        assert result["passed"] is False
        assert result["detail"].startswith("coverage_gap")

    def test_tier2_expected_honest_route_up_passes(self) -> None:
        result = score_case({"case_id": "x", "query": "榴莲多少钱", "expected_layer": "tier2"})
        assert result["passed"] is True
        assert result["detail"] == "诚实上抛"

    def test_tier2_expected_router_route_up_passes_with_exact_layer(self) -> None:
        result = score_case(
            {"case_id": "x", "query": "苏州有哪些供应商", "expected_layer": "tier2"}
        )
        assert result["passed"] is True
        assert result["actual"]["exact_layer"] == "tier2"
        assert result["actual"]["would_grab"] == ["getSupplierByStatus"]

    def test_tier3_expected_honest_route_up_passes(self) -> None:
        result = score_case(
            {"case_id": "x", "query": "明天能到货的订单有哪些", "expected_layer": "tier3"}
        )
        assert result["passed"] is True

    def test_tier3_expected_router_route_up_passes_with_exact_layer(self) -> None:
        result = score_case(
            {"case_id": "x", "query": "苹果和香蕉哪个贵", "expected_layer": "tier3"}
        )
        assert result["passed"] is True
        assert result["actual"]["exact_layer"] == "tier3"


class TestEvaluateCases:
    def test_full_dataset_routing_accuracy(self) -> None:
        output = evaluate_cases(_cases())
        assert output["mode"] == "agent_routing"
        assert len(output["per_case"]) == 50
        passed = sum(1 for pc in output["per_case"] if pc["passed"])
        assert output["primary_score"] == pytest.approx(50 / 50)
        assert passed == 50

    def test_no_overgrab_failures_remain(self) -> None:
        output = evaluate_cases(_cases())
        failed = {pc["case_id"] for pc in output["per_case"] if not pc["passed"]}
        assert failed == set()
        assert all("overgrab" not in pc["detail"] for pc in output["per_case"])

    def test_metrics_reflect_the_router_boundary(self) -> None:
        output = evaluate_cases(_cases())
        metrics = output["metrics"]
        # 20 tier1 grabs (all correct), 30 honest route-ups, zero overgrabs.
        assert metrics["tier1_hit_rate"] == pytest.approx(20 / 50)
        assert metrics["tier1_correct_rate"] == pytest.approx(20 / 20)
        assert metrics["route_up_rate"] == pytest.approx(30 / 50)
        assert metrics["overgrab_rate"] == 0.0
        assert metrics["coverage_gap_rate"] == 0.0
        assert metrics["layer_assign_correct_rate"] == pytest.approx(30 / 30)
        assert metrics["layer_counts"] == {"tier1": 20, "tier2": 15, "tier3": 15}

    def test_route_up_cases_carry_exact_layer_and_would_grab(self) -> None:
        output = evaluate_cases(_cases())
        for pc in output["per_case"]:
            if pc["actual"]["layer"] != "route_up":
                continue
            assert pc["actual"]["exact_layer"] == pc["expected"]["layer"], pc["case_id"]
            assert "would_grab" in pc["actual"], pc["case_id"]
