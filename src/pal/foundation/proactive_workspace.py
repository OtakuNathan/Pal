"""Stable data-path compatibility for the managed proactive workspace."""
from __future__ import annotations

from pathlib import Path

LEGACY_PROACTIVE_DATA = (
    "english_learning",
    "nutrition",
    "leetcode_history.json",
    "leetcode_coaching.jsonl",
)


def proactive_workspace_aliases(runtime_root: Path, worktree: Path) -> tuple[tuple[Path, Path], ...]:
    return ((runtime_root / "proactive", worktree), *(
        (runtime_root / "data" / name, worktree / name) for name in LEGACY_PROACTIVE_DATA
    ))
