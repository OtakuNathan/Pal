"""Execution state shared by resident and role hosts; no service dependencies."""
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTurnRuntimeState:
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    pending_channel_turns: deque[Any] = field(default_factory=deque)
    resident_execution_lifetime_id: str = ""
