from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pal.control.contracts import ControlRoute
from pal.foundation import utc_now


@dataclass
class PendingControlRequest:
    request_id: str
    request_kind: str
    control_scope_key: str
    route: ControlRoute
    created_at: str = field(default_factory=utc_now)
    expires_at: str = field(default_factory=utc_now)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ControlScopeState:
    active_turn_id: str | None = None
    pending_channel_turns: deque[Any] = field(default_factory=deque)
    pending_requests: dict[str, PendingControlRequest] = field(default_factory=dict)
    transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    interrupt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    quiescing: bool = False
    drained_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupting_turn_id: str | None = None
    interrupt_task: asyncio.Task[bool] | None = None

    def __post_init__(self) -> None:
        if self.active_turn_id is None:
            self.drained_event.set()


@dataclass
class CoreRuntimeState:
    active_turns: dict[str, Any] = field(default_factory=dict)
    completed_turns: dict[str, Any] = field(default_factory=dict)
    turn_tasks: dict[str, Any] = field(default_factory=dict)
    turn_scopes: dict[str, str] = field(default_factory=dict)
    control_scopes: dict[str, ControlScopeState] = field(default_factory=dict)
    prompt_log_enabled: bool = False
    mode: str = "default"
    detached_modules: set[str] = field(default_factory=set)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
