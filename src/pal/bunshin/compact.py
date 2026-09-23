"""Bunshin host adapter for the shared continuity checkpoint."""
from dataclasses import dataclass

from pal.core.compaction import CompactionClockKind
from pal.core.continuity_compaction import ContinuityCompactionPolicy, system_prompt

BUNSHIN_COMPACTION_SYSTEM_PROMPT = system_prompt("bunshin")


@dataclass(frozen=True)
class BunshinCompactionPolicy(ContinuityCompactionPolicy):
    kind: str = "bunshin"
    clock_kind: CompactionClockKind = CompactionClockKind.LLM_ROUND
    accepts_memory_candidates: bool = False


__all__ = ["BUNSHIN_COMPACTION_SYSTEM_PROMPT", "BunshinCompactionPolicy"]
