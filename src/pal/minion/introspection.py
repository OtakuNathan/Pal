from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pal.minion.runtime import MinionRuntime


@dataclass(frozen=True)
class MinionSnapshot:
    accepted_contexts: int


class MinionIntrospection(Protocol):
    def snapshot(self) -> MinionSnapshot:
        ...


def inspect_minion(runtime: MinionRuntime) -> MinionSnapshot:
    return MinionSnapshot(accepted_contexts=len(runtime.accepted_contexts))


# Compatibility aliases.
WorkerSnapshot = MinionSnapshot
WorkerIntrospection = MinionIntrospection
inspect_worker = inspect_minion

