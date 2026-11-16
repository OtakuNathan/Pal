"""Host-neutral compaction execution contract."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol

from pal.llm.contracts import LLMService

if TYPE_CHECKING:
    from pal.llm.compaction_engine import (
        CompactionCommitHooks,
        CompactionPolicy,
        CompactionRunResult,
        CompactionSnapshot,
    )


class CompactionService(Protocol):
    policy: CompactionPolicy
    max_attempts: int
    timeout_seconds: float

    def with_limits(self, *, max_attempts: int | None = None,
                    timeout_seconds: float | None = None) -> CompactionService: ...

    async def run(self, snapshot: CompactionSnapshot, *, llm_runtime: LLMService,
                  replay_guard: Callable[[], bool] | None = None,
                  commit_hooks: CompactionCommitHooks | None = None) -> CompactionRunResult: ...
