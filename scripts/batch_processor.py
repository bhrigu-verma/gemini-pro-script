"""Generic completion-loop controller for target-based automation tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.history import load_history


@dataclass
class CompletionController:
    target_count: int
    max_iterations: int
    max_empty_streak: int

    iteration: int = 0
    accepted_total: int = 0
    empty_streak: int = 0

    def should_continue(self) -> bool:
        if self.accepted_total >= self.target_count:
            return False
        if self.iteration >= self.max_iterations:
            return False
        if self.empty_streak >= self.max_empty_streak:
            return False
        return True

    def register_round(self, accepted_count: int) -> None:
        self.iteration += 1
        self.accepted_total += max(0, accepted_count)
        if accepted_count > 0:
            self.empty_streak = 0
        else:
            self.empty_streak += 1

    @property
    def remaining(self) -> int:
        return max(0, self.target_count - self.accepted_total)

    @property
    def stop_reason(self) -> str:
        if self.accepted_total >= self.target_count:
            return "target_reached"
        if self.iteration >= self.max_iterations:
            return "max_iterations_reached"
        if self.empty_streak >= self.max_empty_streak:
            return "max_empty_streak_reached"
        return "running"


def controller_from_history(
    history_path: Path,
    target_count: int,
    max_iterations: int,
    max_empty_streak: int,
) -> tuple[CompletionController, dict[str, Any] | None]:
    """Create a completion controller with accepted_total restored from persisted state."""
    state = load_history(history_path)
    accepted_total = 0
    if isinstance(state, dict):
        records = state.get("records", [])
        if isinstance(records, list):
            accepted_total = len(records)

    controller = CompletionController(
        target_count=target_count,
        max_iterations=max_iterations,
        max_empty_streak=max_empty_streak,
        accepted_total=accepted_total,
    )
    return controller, state
