"""Validation tests for the 200-case stratified planning dataset — task 7.10.

planning_200.json extends the LLM planner eval from 50 to 200 cases, stratified
into four layers (single/multi × easy/hard, 50 each — every layer ≥ 30 so a
per-layer confidence interval is meaningful). This file asserts:

- dataset structure and seed grounding: each expected tool set must be
  reachable through the deterministic classify_intent → filter_candidates
  chain, otherwise the LLM could never select it and the case would measure a
  data error instead of model capability;
- the original 50 baseline cases (plan-001..050) are preserved in tool terms,
  so drift comparison stays apples-to-apples against the 50-case baseline
  report;
- the new scoring machinery in evals/llm_planner_eval: the Wilson 95%
  confidence interval and the baseline drift delta + alarm.

No LLM and no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from apps.erp_simulator.data.products import SEED_PRODUCTS
from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.nodes.validate_plan import ToolSpec
from erp_copilot.tools.candidate_filter import V6_TOOL_NAMES, filter_candidates
from evals.llm_planner_eval import (
    baseline_overlap_metrics,
    compute_drift,
    evaluate_cases,
    load_baseline_report,
    wilson_ci,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATASETS = PROJECT_ROOT / "evals" / "datasets"

PLANNING_200 = DATASETS / "planning_200.json"
PLANNING_50 = DATASETS / "planning_50.json"

REGIONS = {"上海", "南京", "北京", "天津", "广州", "深圳", "成都", "重庆", "西安", "兰州"}
PRODUCT_NAMES = {p.name for p in SEED_PRODUCTS}

LAYER_NAMES = {"single_easy", "single_hard", "multi_easy", "multi_hard"}
MIN_LAYER_SIZE = 30


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases() -> list[dict]:
    return _load(PLANNING_200)["cases"]


def _layer(case: dict) -> str:
    return f"{'single' if case['single_step'] else 'multi'}_{case['difficulty']}"


class TestDataset200:
    def test_file_exists_with_metadata(self) -> None:
        data = _load(PLANNING_200)
        assert data["description"]
        assert data["version"]
        assert len(data["cases"]) == 200

    def test_case_ids_unique(self) -> None:
        ids = [c["case_id"] for c in _cases()]
        assert len(ids) == len(set(ids))

    def test_case_ids_cover_original_and_new(self) -> None:
        """plan-001..plan-200 sequential; the original 50 are folded in."""
        ids = [c["case_id"] for c in _cases()]
        assert ids == [f"plan-{i:03d}" for i in range(1, 201)]

    def test_schema_required_fields(self) -> None:
        for case in _cases():
            required = {"case_id", "query", "steps", "single_step", "required_params", "difficulty"}
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"note"}, case["case_id"]
            assert isinstance(case["single_step"], bool), case["case_id"]
            assert case["difficulty"] in {"easy", "hard"}, case["case_id"]
            assert len(case["steps"]) >= 1, case["case_id"]
            assert case["single_step"] == (len(case["steps"]) == 1), case["case_id"]
            assert isinstance(case["required_params"], list), case["case_id"]
            for step in case["steps"]:
                assert step["tool"] in V6_TOOL_NAMES, f"{case['case_id']}: {step['tool']}"
                assert isinstance(step["params"], dict), case["case_id"]

    def test_dependency_refs_point_backward(self) -> None:
        for case in _cases():
            for index, step in enumerate(case["steps"], start=1):
                for value in step["params"].values():
                    if isinstance(value, str):
                        for ref in re.findall(r"\$(\d+)\.", value):
                            assert int(ref) < index, (
                                f"{case['case_id']} step {index}: ref ${ref} not backward"
                            )

    def test_first_step_has_no_forward_dependency(self) -> None:
        for case in _cases():
            for value in case["steps"][0]["params"].values():
                assert not isinstance(value, str) or "$" not in value, case["case_id"]

    def test_every_layer_has_at_least_30_cases(self) -> None:
        counts: dict[str, int] = {}
        for case in _cases():
            counts[_layer(case)] = counts.get(_layer(case), 0) + 1
        assert set(counts) == LAYER_NAMES
        for layer, count in counts.items():
            assert count >= MIN_LAYER_SIZE, f"{layer} has {count} (< {MIN_LAYER_SIZE})"

    def test_original_50_preserved_in_tool_terms(self) -> None:
        """The baseline overlap subset must be bit-identical in expected tools.

        Drift compares current run vs the 50-case baseline report on the shared
        case ids; if the expected plans for plan-001..050 changed, the delta
        would mix dataset edits with model drift.
        """
        old = {c["case_id"]: c for c in _load(PLANNING_50)["cases"]}
        new = {c["case_id"]: c for c in _cases()}
        assert set(old) == {f"plan-{i:03d}" for i in range(1, 51)}
        for case_id, original in old.items():
            updated = new[case_id]
            assert [s["tool"] for s in updated["steps"]] == [s["tool"] for s in original["steps"]]
            assert updated["single_step"] == original["single_step"]

    def test_expected_tools_reachable_through_deterministic_chain(self) -> None:
        """Grounding: expected tools ⊆ filter_candidates(classify_intent(query)).

        The eval runs the real classifier then hands the LLM only the filtered
        candidate set; a case whose expected plan is outside it is a data bug.
        """
        for case in _cases():
            intent = classify_intent(case["query"])
            candidates = filter_candidates(intent.domain, intent.action)
            expected = {step["tool"] for step in case["steps"]}
            assert expected <= set(candidates), (
                f"{case['case_id']}: expected {expected} not in candidates {candidates}"
            )

    def test_step_params_grounded_in_seed_data(self) -> None:
        for case in _cases():
            for step in case["steps"]:
                params = step["params"]
                if step["tool"] == "getProductByName":
                    assert params["name"] in PRODUCT_NAMES, (
                        f"{case['case_id']}: unknown product {params['name']}"
                    )
                if step["tool"] == "querySuppliersByDeliveryRegion":
                    assert params["region"] in REGIONS, (
                        f"{case['case_id']}: unknown region {params['region']}"
                    )
                if step["tool"] == "createOrder":
                    quantity = params["quantity"]
                    if not (isinstance(quantity, str) and "$" in quantity):
                        # A dependency reference (e.g. $1.quantity in a
                        # cancel-and-recreate chain) substitutes a literal at
                        # execution time; only standalone literals must be > 0.
                        assert isinstance(quantity, int) and quantity > 0, case["case_id"]
                    region = params.get("region")
                    if region is not None and isinstance(region, str) and "$" not in region:
                        assert region in REGIONS, case["case_id"]

    def test_intent_spread_across_layers(self) -> None:
        """No layer is a single-intent monoculture."""
        per_layer_intents: dict[str, set[str]] = {}
        for case in _cases():
            intent = classify_intent(case["query"])
            per_layer_intents.setdefault(_layer(case), set()).add(
                f"{intent.domain}/{intent.action}"
            )
        for layer, intents in per_layer_intents.items():
            assert len(intents) >= 2, f"{layer} only covers {intents}"


class TestWilsonCI:
    def test_n_zero_returns_zero_interval(self) -> None:
        assert wilson_ci(0.5, 0) == (0.0, 0.0)

    def test_rate_is_clamped_to_unit_interval(self) -> None:
        lo, hi = wilson_ci(1.3, 50)
        assert 0.0 <= lo <= hi <= 1.0

    def test_interval_contains_point_estimate(self) -> None:
        for rate, n in [(0.5, 50), (0.98, 50), (0.1, 200), (1.0, 30)]:
            lo, hi = wilson_ci(rate, n)
            assert lo <= rate <= hi, (rate, n, lo, hi)
            assert lo >= 0.0 and hi <= 1.0

    def test_width_shrinks_with_sample_size(self) -> None:
        def half(rate: float, n: int) -> float:
            lo, hi = wilson_ci(rate, n)
            return (hi - lo) / 2

        assert half(0.5, 50) > half(0.5, 200)
        assert half(1.0, 50) > half(1.0, 200)

    def test_perfect_score_interval_is_wide_on_small_sample(self) -> None:
        # p=1, n=50: Wilson pulls the interval down off 1.0 — 50/50 on a 50-case
        # layer is not proof of a 100% capability (still ±~4.4%).
        lo, hi = wilson_ci(1.0, 50)
        assert 0.9 <= lo < 1.0
        assert hi == 1.0


class TestDrift:
    def _baseline(self) -> dict:
        return {
            "case_ids": {f"plan-{i:03d}" for i in range(1, 51)},
            "metrics": {
                "num_cases": 50,
                "tool_set_exact_rate": 0.98,
                "tool_seq_exact_rate": 0.98,
                "contract_valid_rate": 0.86,
                "parse_success_rate": 1.0,
            },
        }

    def test_overlap_metrics_restrict_to_shared_cases(self) -> None:
        per_case = [
            {"case_id": "plan-001", "tool_set_exact": True, "tool_seq_exact": True,
             "contract_valid": True, "parse_success": True},
            {"case_id": "plan-002", "tool_set_exact": False, "tool_seq_exact": False,
             "contract_valid": True, "parse_success": True},
            {"case_id": "plan-201", "tool_set_exact": False, "tool_seq_exact": False,
             "contract_valid": False, "parse_success": True},
        ]
        overlap = baseline_overlap_metrics(per_case, {"plan-001", "plan-002"})
        assert overlap["num_cases"] == 2
        assert overlap["tool_set_exact_rate"] == 0.5
        # The out-of-baseline case must not influence the overlap rates.
        assert overlap["parse_success_rate"] == 1.0

    def test_delta_is_current_minus_baseline(self) -> None:
        overlap = {"num_cases": 50, "tool_set_exact_rate": 0.96}
        drift = compute_drift(overlap, self._baseline())
        assert drift["tool_set_exact_delta"] == -0.02
        assert drift["alarm"] is False  # exactly at threshold is not a drop

    def test_alarm_when_drop_exceeds_threshold(self) -> None:
        overlap = {"num_cases": 50, "tool_set_exact_rate": 0.94}
        drift = compute_drift(overlap, self._baseline())
        assert drift["tool_set_exact_delta"] == -0.04
        assert drift["alarm"] is True

    def test_no_alarm_when_improved(self) -> None:
        overlap = {"num_cases": 50, "tool_set_exact_rate": 1.0}
        drift = compute_drift(overlap, self._baseline())
        assert drift["tool_set_exact_delta"] == 0.02
        assert drift["alarm"] is False

    def test_no_alarm_without_baseline(self) -> None:
        overlap = {"num_cases": 50, "tool_set_exact_rate": 0.9}
        drift = compute_drift(overlap, None)
        assert drift["alarm"] is False
        assert drift["status"] == "no_baseline"

    def test_load_baseline_report_missing_file(self) -> None:
        assert load_baseline_report(DATASETS / "does_not_exist.json") is None


class TestLayerAggregation:
    TOOLS: dict[str, ToolSpec] = {
        "getProductByName": ToolSpec(name="getProductByName", required_params=["name"]),
        "getProductById": ToolSpec(name="getProductById", required_params=["id"]),
        "getProductSubstitutesByName": ToolSpec(
            name="getProductSubstitutesByName", required_params=["name"]
        ),
    }

    @staticmethod
    def _case(case_id: str, query: str, tool: str, difficulty: str) -> dict:
        return {
            "case_id": case_id,
            "query": query,
            "single_step": True,
            "difficulty": difficulty,
            "steps": [{"tool": tool, "params": {}}],
        }

    def _run(self, cases: list[dict], responses: list[str]) -> dict:
        iterator = iter(responses)

        def fake_llm(_prompt: str) -> str:
            return next(iterator)

        return evaluate_cases(
            cases,
            llm_complete=fake_llm,
            available_tools=set(self.TOOLS),
            tool_schemas=self.TOOLS,
        )

    def test_layers_aggregate_by_difficulty(self) -> None:
        correct = (
            '{"steps": [{"step_id": "1", "tool_name": "getProductByName",'
            ' "arguments": {"name": "苹果"}, "risk_level": "READ"}]}'
        )
        wrong = (
            '{"steps": [{"step_id": "1", "tool_name": "getProductById",'
            ' "arguments": {"id": 1}, "risk_level": "READ"}]}'
        )
        cases = [
            self._case("e1", "苹果多少钱", "getProductByName", "easy"),
            self._case("e2", "香蕉多少钱", "getProductByName", "easy"),
            self._case("h1", "苹果的替代品", "getProductSubstitutesByName", "hard"),
            self._case("h2", "键盘的替代品", "getProductSubstitutesByName", "hard"),
        ]
        out = self._run(cases, [correct, correct, wrong, wrong])

        easy = out["layers"]["single_easy"]
        assert easy["num_cases"] == 2
        assert easy["tool_set_exact_rate"] == 1.0
        hard = out["layers"]["single_hard"]
        assert hard["num_cases"] == 2
        assert hard["tool_set_exact_rate"] == 0.0
        # A 0/2 layer still gets an honest interval, not a degenerate one.
        assert hard["ci_lower"] == 0.0
        assert hard["ci_upper"] > 0.0
        assert set(out["layers"]) == {"single_easy", "single_hard"}

    def test_missing_difficulty_defaults_to_easy(self) -> None:
        correct = (
            '{"steps": [{"step_id": "1", "tool_name": "getProductByName",'
            ' "arguments": {"name": "苹果"}, "risk_level": "READ"}]}'
        )
        case = {
            "case_id": "legacy",
            "query": "苹果多少钱",
            "single_step": True,
            "steps": [{"tool": "getProductByName", "params": {}}],
        }
        out = self._run([case], [correct])
        assert out["per_case"][0]["difficulty"] == "easy"
        assert set(out["layers"]) == {"single_easy"}

    def test_all_four_layers_present_when_mixed(self) -> None:
        correct_single = (
            '{"steps": [{"step_id": "1", "tool_name": "getProductByName",'
            ' "arguments": {"name": "苹果"}, "risk_level": "READ"}]}'
        )
        correct_multi = (
            '{"steps": ['
            ' {"step_id": "1", "tool_name": "getProductByName",'
            '  "arguments": {"name": "苹果"}, "risk_level": "READ"},'
            ' {"step_id": "2", "tool_name": "querySuppliersByDeliveryRegion",'
            '  "arguments": {"region": "上海"}, "risk_level": "READ"},'
            ' {"step_id": "3", "tool_name": "createOrder",'
            '  "arguments": {"product_id": 1, "supplier_id": 2,'
            '   "quantity": 5, "region": "上海"}, "risk_level": "WRITE",'
            '  "fallback": "取消新建订单以补偿"}]}'
        )
        multi_case = {
            "case_id": "m1",
            "query": "帮我在上海下一单 10 KG 苹果",
            "single_step": False,
            "difficulty": "hard",
            "steps": [
                {"tool": "getProductByName", "params": {"name": "苹果"}},
                {"tool": "querySuppliersByDeliveryRegion", "params": {"region": "上海"}},
                {
                    "tool": "createOrder",
                    "params": {
                        "product_id": "$1.product_id",
                        "supplier_id": "$2.supplier_id",
                        "quantity": 10,
                        "region": "上海",
                    },
                },
            ],
        }
        cases = [
            self._case("e1", "苹果多少钱", "getProductByName", "easy"),
            multi_case,
        ]
        tools = dict(self.TOOLS)
        tools["querySuppliersByDeliveryRegion"] = ToolSpec(
            name="querySuppliersByDeliveryRegion", required_params=["region"]
        )
        tools["createOrder"] = ToolSpec(
            name="createOrder",
            required_params=["product_id", "supplier_id", "quantity", "region"],
        )
        responses = iter([correct_single, correct_multi])

        def fake_llm(_prompt: str) -> str:
            return next(responses)

        out = evaluate_cases(
            cases,
            llm_complete=fake_llm,
            available_tools=set(tools),
            tool_schemas=tools,
        )
        assert set(out["layers"]) == {"single_easy", "multi_hard"}
        assert out["layers"]["multi_hard"]["num_cases"] == 1
        assert out["layers"]["multi_hard"]["tool_set_exact_rate"] == 1.0
