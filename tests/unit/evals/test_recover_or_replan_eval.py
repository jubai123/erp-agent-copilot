"""Unit tests for the recover_or_replan eval module — task 6.6 upgrade.

The dataset seeds map a recover_or_replan_20.json case onto an AgentState and
the runner calls the real recover_or_replan node (src/erp_copilot/agent/nodes/
recover_or_replan.py) — the node hard-wired in build_agent_graph — instead of
the harness-only decide_recovery_action / decide_failure_behavior modules.
"""

from __future__ import annotations

import json

from erp_copilot.agent.state import AgentStatus, Plan, PlanStep, StateError, StepResult
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel
from evals.harness import DATASET_DIR
from evals.recover_or_replan_eval import build_seed_state, evaluate_cases


def _load_cases() -> list[dict]:
    data = json.loads((DATASET_DIR / "recover_or_replan_20.json").read_text(encoding="utf-8"))
    return data["cases"]


class TestBuildSeedState:
    def test_base_fields_default(self) -> None:
        seed = {
            "status": "EXECUTING",
            "retry_count": 0,
            "replan_count": 0,
            "errors": [],
            "plan": None,
            "step_results": [],
        }
        state = build_seed_state(seed, case_id="rec-node-000")
        assert state.run_id == "eval-rec-node-000"
        assert state.tenant_id == "eval"
        assert state.status is AgentStatus.EXECUTING
        assert state.retry_count == 0
        assert state.replan_count == 0
        assert state.errors == []
        assert state.plan is None
        assert state.step_results == {}

    def test_status_maps_to_agent_status(self) -> None:
        for raw, expected in (
            ("RETRYING", AgentStatus.RETRYING),
            ("REPLANNING", AgentStatus.REPLANNING),
            ("PLANNING", AgentStatus.PLANNING),
            ("SUCCEEDED", AgentStatus.SUCCEEDED),
        ):
            seed = {
                "status": raw,
                "retry_count": 0,
                "replan_count": 0,
                "errors": [],
                "plan": None,
                "step_results": [],
            }
            assert build_seed_state(seed, case_id="c").status is expected

    def test_errors_map_to_state_error(self) -> None:
        seed = {
            "status": "RETRYING",
            "retry_count": 0,
            "replan_count": 0,
            "errors": [
                {"code": "TIMEOUT", "message": "request timed out", "step_id": "s1"},
                {"code": "UNKNOWN_TOOL", "message": "tool not in registry"},
            ],
            "plan": None,
            "step_results": [],
        }
        state = build_seed_state(seed, case_id="c")
        assert state.errors == [
            StateError(code="TIMEOUT", message="request timed out", step_id="s1"),
            StateError(code="UNKNOWN_TOOL", message="tool not in registry"),
        ]

    def test_plan_maps_to_plan_with_step_models(self) -> None:
        seed = {
            "status": "RETRYING",
            "retry_count": 0,
            "replan_count": 0,
            "errors": [{"code": "TIMEOUT", "message": "request timed out", "step_id": "s1"}],
            "plan": {
                "steps": [
                    {
                        "step_id": "s1",
                        "tool_name": "createOrder",
                        "risk_level": "WRITE",
                        "idempotency_key": "eval-rec-node-001:s1",
                    }
                ]
            },
            "step_results": [],
        }
        state = build_seed_state(seed, case_id="c")
        assert state.plan == Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="createOrder",
                    risk_level=ToolRiskLevel.WRITE,
                    idempotency_key="eval-rec-node-001:s1",
                )
            ]
        )

    def test_step_results_map_by_step_id(self) -> None:
        seed = {
            "status": "RETRYING",
            "retry_count": 0,
            "replan_count": 0,
            "errors": [{"code": "TIMEOUT", "message": "request timed out", "step_id": "s2"}],
            "plan": None,
            "step_results": [
                {"step_id": "s1", "status": "COMPLETED", "error_code": None},
                {
                    "step_id": "s2",
                    "status": "FAILED",
                    "error_code": "TIMEOUT",
                    "is_retryable": True,
                },
            ],
        }
        state = build_seed_state(seed, case_id="c")
        assert state.step_results == {
            "s1": StepResult(step_id="s1", status=StepStatus.COMPLETED),
            "s2": StepResult(
                step_id="s2",
                status=StepStatus.FAILED,
                error_code="TIMEOUT",
                is_retryable=True,
            ),
        }


class TestEvaluateCases:
    def test_real_dataset_scores_perfect(self) -> None:
        output = evaluate_cases(_load_cases())
        assert output["mode"] == "deterministic"
        assert output["primary_score"] == 1.0
        assert output["metrics"]["decision_accuracy"] == 1.0
        assert len(output["per_case"]) == 20
        assert all(pc["passed"] for pc in output["per_case"])

    def test_deliberately_wrong_expected_fails(self) -> None:
        # rec-node-001 expects EXECUTING + retry=1; flip it to FAILED to prove
        # the runner asserts the terminal state rather than always passing.
        wrong = json.loads(json.dumps(_load_cases()))
        wrong[0]["expected"]["status"] = "FAILED"
        output = evaluate_cases(wrong)
        assert output["primary_score"] == 19 / 20
        assert output["per_case"][0]["passed"] is False
        assert output["per_case"][0]["actual"]["status"] == "EXECUTING"
        assert output["per_case"][0]["actual"]["retry_count"] == 1
