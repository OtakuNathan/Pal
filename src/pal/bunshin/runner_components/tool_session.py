from __future__ import annotations
from dataclasses import dataclass, field
from pal.bunshin.runner_components.contracts import RoleExecutionSessions


@dataclass
class ToolSession:
    visible_capability_aliases: list[str] = field(default_factory=list, init=False, repr=False)
    observed_tool_call_count: int = field(default=0, init=False, repr=False)
    execution_sessions: RoleExecutionSessions | None = field(default=None, init=False, repr=False)

    def bind_execution(self, sessions: RoleExecutionSessions | None) -> None:
        self.execution_sessions = sessions

    def bind_capabilities(self, aliases: list[str]) -> None:
        self.visible_capability_aliases = list(aliases)

    def observe_count(self, count: int) -> None:
        self.observed_tool_call_count = max(self.observed_tool_call_count, count)
