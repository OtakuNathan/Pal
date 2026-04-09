from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from pal.foundation.persistence import utc_now


@dataclass(frozen=True)
class ControlEvent:
    event_kind: str
    source_kind: str
    payload: dict[str, Any]
    response_handle: dict[str, Any] | None = None
    correlation_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class ControlAction:
    action_kind: str
    target_scope: str
    target_id: str | None = None
    requires_user_confirmation: bool = False
    notes: str = ""


class ControlPlanePort(Protocol):
    def parse_event(self, event: ControlEvent) -> ControlAction | None:
        ...
