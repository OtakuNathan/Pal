"""Explicit optional lifecycle interface for resident-context side requests."""
from __future__ import annotations

from dataclasses import dataclass

from pal.llm.contracts import LLMSideTurn
from pal.llm.runtime import LLMRuntime


@dataclass
class LLMSideTurnService:
    runtime: LLMRuntime

    def create_side_turn(
        self, *, scope_id: str, branch_id: str, message_ids: tuple[str, ...],
    ) -> LLMSideTurn:
        return self.runtime.fork_side_turn(scope_id, branch_id, message_ids)
