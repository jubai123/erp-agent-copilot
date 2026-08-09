"""Unit tests for LLM Record/Replay — task 6.7.

The module wraps the injected ``llm_complete`` seam (prompt -> completion) so a
live run can be recorded once and replayed offline. These tests exercise
record, replay, and off modes plus the JSONL file format with a fake LLM
callable — no network, no paid provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.record_replay import RecordReplay, ReplayMissError, load_recording


def _echo_llm(prompt: str) -> str:
    return f"completion-of:{prompt}"


class TestOffMode:
    def test_returns_original_callable(self) -> None:
        recorder = RecordReplay(Path("unused.jsonl"), "off")
        assert recorder.wrap(_echo_llm) is _echo_llm

    def test_requires_callable(self) -> None:
        recorder = RecordReplay(Path("unused.jsonl"), "off")
        with pytest.raises(ValueError, match="off mode"):
            recorder.wrap()


class TestRecordMode:
    def test_returns_real_completion_and_records(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        recorder = RecordReplay(path, "record")
        wrapped = recorder.wrap(_echo_llm)
        assert wrapped("hello") == "completion-of:hello"
        recorder.save()
        assert load_recording(path) == {"hello": "completion-of:hello"}

    def test_records_each_call(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        recorder = RecordReplay(path, "record")
        wrapped = recorder.wrap(_echo_llm)
        wrapped("a")
        wrapped("b")
        recorder.save()
        assert load_recording(path) == {"a": "completion-of:a", "b": "completion-of:b"}

    def test_requires_callable(self, tmp_path) -> None:
        recorder = RecordReplay(tmp_path / "rec.jsonl", "record")
        with pytest.raises(ValueError, match="record mode"):
            recorder.wrap()

    def test_save_dedupes_and_is_idempotent(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        recorder = RecordReplay(path, "record")
        wrapped = recorder.wrap(_echo_llm)
        wrapped("a")
        recorder.save()
        wrapped("a")
        recorder.save()
        assert load_recording(path) == {"a": "completion-of:a"}


class TestReplayMode:
    def test_returns_recorded_completion_without_calling(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        recorder = RecordReplay(path, "record")
        wrapped = recorder.wrap(_echo_llm)
        wrapped("hello")
        recorder.save()

        def _should_never_be_called(prompt: str) -> str:
            raise AssertionError("real LLM must not be called in replay mode")

        replayer = RecordReplay(path, "replay")
        replayed = replayer.wrap(_should_never_be_called)  # provider is ignored
        assert replayed("hello") == "completion-of:hello"

    def test_missing_prompt_raises_replay_miss(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        recorder = RecordReplay(path, "record")
        recorder.wrap(_echo_llm)("hello")
        recorder.save()
        replayer = RecordReplay(path, "replay")
        replayed = replayer.wrap()
        with pytest.raises(ReplayMissError):
            replayed("goodbye")

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            RecordReplay(tmp_path / "nope.jsonl", "replay")

    def test_duplicate_prompt_first_wins(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"prompt": "p", "completion": "first"}\n{"prompt": "p", "completion": "second"}\n',
            encoding="utf-8",
        )
        replayer = RecordReplay(path, "replay")
        assert replayer.wrap()("p") == "first"
        assert load_recording(path) == {"p": "first"}


class TestRoundTripAndSeam:
    def test_record_then_replay_end_to_end(self, tmp_path) -> None:
        path = tmp_path / "rec.jsonl"
        calls: list[str] = []

        def provider(prompt: str) -> str:
            calls.append(prompt)
            return f"llm({prompt})"

        recorder = RecordReplay(path, "record")
        wrapped = recorder.wrap(provider)
        prompts = ["a", "b"]
        completions = [wrapped(p) for p in prompts]
        recorder.save()
        assert calls == prompts

        calls.clear()
        replayer = RecordReplay(path, "replay")
        replayed = replayer.wrap()
        assert [replayed(p) for p in prompts] == completions
        assert calls == []  # provider untouched in replay

    def test_invalid_mode_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="invalid mode"):
            RecordReplay(tmp_path / "rec.jsonl", "bogus")

    def test_wrapped_callable_plugs_into_build_plan_seam(self, tmp_path) -> None:
        """The wrapped callable is accepted as llm_complete by build_plan_node."""
        from erp_copilot.agent.nodes.build_plan import build_plan_node
        from erp_copilot.agent.state import AgentState, IntentClassification

        def _plan_llm(prompt: str) -> str:
            return json.dumps(
                {
                    "title": "plan",
                    "steps": [
                        {
                            "step_id": "s1",
                            "tool_name": "getProductByName",
                            "depends_on": [],
                            "description": "查询商品",
                            "arguments": {"name": "苹果"},
                            "argument_sources": {"name": "user_query"},
                            "risk_level": "READ",
                        }
                    ],
                }
            )

        path = tmp_path / "rec.jsonl"
        recorder = RecordReplay(path, "record")
        node = build_plan_node(llm_complete=recorder.wrap(_plan_llm))
        state = AgentState(
            run_id="run-1",
            tenant_id="t1",
            query="下单 10 KG 苹果到上海",
            intent=IntentClassification(domain="order", action="create"),
        )
        result = node(state)
        assert result["plan"] is not None
        assert result["plan"].title == "plan"
        recorder.save()
        recording = load_recording(path)
        assert any("下单 10 KG 苹果到上海" in prompt for prompt in recording)
