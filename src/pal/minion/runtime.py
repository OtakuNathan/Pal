from __future__ import annotations

from dataclasses import dataclass, field

from pal.minion.contracts import MinionIPCHandlers, MinionRuntimePort
from pal.shared import TaskContextPack


@dataclass
class MinionRuntime(MinionRuntimePort):
    handlers: MinionIPCHandlers = field(default_factory=MinionIPCHandlers)
    accepted_contexts: list[TaskContextPack] = field(default_factory=list)

    def accept(self, context_pack: TaskContextPack) -> None:
        self.accepted_contexts.append(context_pack)

