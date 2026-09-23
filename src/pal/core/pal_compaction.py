"""Resident host adapter for the shared continuity checkpoint."""
from dataclasses import dataclass

from pal.core.continuity_compaction import ContinuityCompactionPolicy, system_prompt

COMPACT_PAL_STRUCTURED_SYSTEM = system_prompt("pal")


@dataclass(frozen=True)
class PalCompactionPolicy(ContinuityCompactionPolicy):
    pass


__all__ = ["COMPACT_PAL_STRUCTURED_SYSTEM", "PalCompactionPolicy"]
