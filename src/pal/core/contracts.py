from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CoreRuntimeState:
    active_turns: dict[str, Any] = field(default_factory=dict)
    completed_turns: dict[str, Any] = field(default_factory=dict)
    mode: str = "default"
    detached_modules: set[str] = field(default_factory=set)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)