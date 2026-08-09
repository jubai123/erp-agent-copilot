"""LLM Record/Replay — task 6.7.

Wraps the injected ``llm_complete`` seam (prompt -> completion) so a live run
can be recorded once and replayed offline. Modes:

- ``record``: calls the real provider, buffers (prompt, completion), and
  ``save()`` writes the buffer to a JSONL file.
- ``replay``: serves recorded completions by exact prompt match; a miss raises
  :class:`ReplayMissError` so stale recordings fail loudly in CI. The provider
  is never called.
- ``off``: returns the wrapped callable unchanged.

JSONL format: one ``{"prompt": ..., "completion": ...}`` object per line.
Replay and :func:`load_recording` are first-wins on duplicate prompts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

Prompt = str
Completion = str


class ReplayMissError(KeyError):
    """Raised in replay mode when a prompt was never recorded."""


def load_recording(path: Path) -> dict[Prompt, Completion]:
    """Read a JSONL recording file into a prompt->completion dict (first-wins)."""
    recording: dict[Prompt, Completion] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        recording.setdefault(entry["prompt"], entry["completion"])
    return recording


class RecordReplay:
    """Record, replay, or pass through an ``llm_complete`` callable."""

    MODES = ("record", "replay", "off")

    def __init__(self, path: Path, mode: str) -> None:
        if mode not in self.MODES:
            raise ValueError(f"invalid mode {mode!r}; expected one of {self.MODES}")
        self._path = path
        self._mode = mode
        self._buffer: dict[Prompt, Completion] = {}
        self._recording = load_recording(path) if mode == "replay" else {}

    def wrap(
        self, llm_complete: Callable[[Prompt], Completion] | None = None
    ) -> Callable[[Prompt], Completion]:
        """Return a callable honoring the configured mode."""
        if self._mode == "off":
            if llm_complete is None:
                raise ValueError("off mode requires the original callable to wrap")
            return llm_complete
        if self._mode == "record":
            if llm_complete is None:
                raise ValueError("record mode requires the real callable to wrap")
            return self._record(llm_complete)
        return self._replay()

    def save(self) -> None:
        """Merge buffered calls into the recording file (deduplicated, idempotent)."""
        if not self._buffer:
            return
        try:
            existing = load_recording(self._path)
        except FileNotFoundError:
            existing = {}
        merged = {**self._buffer, **existing}
        self._path.write_text(
            "\n".join(
                json.dumps({"prompt": p, "completion": c}, ensure_ascii=False)
                for p, c in merged.items()
            ),
            encoding="utf-8",
        )
        self._buffer.clear()

    def _record(
        self, llm_complete: Callable[[Prompt], Completion]
    ) -> Callable[[Prompt], Completion]:
        def wrapped(prompt: Prompt) -> Completion:
            completion = llm_complete(prompt)
            self._buffer[prompt] = completion
            return completion

        return wrapped

    def _replay(self) -> Callable[[Prompt], Completion]:
        def wrapped(prompt: Prompt) -> Completion:
            if prompt not in self._recording:
                raise ReplayMissError(prompt)
            return self._recording[prompt]

        return wrapped
