from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.foundation.persistence import utc_now


@dataclass(frozen=True)
class EventEnvelope:
    event_kind: str
    source_kind: str
    payload: Any
    correlation_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: str(uuid4()))
