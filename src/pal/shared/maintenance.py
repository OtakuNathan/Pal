"""Opaque ownership of one resident maintenance admission."""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(frozen=True, eq=False)
class MaintenanceToken:
    token_id: str = field(default_factory=lambda: uuid4().hex)


def owns_maintenance(current: MaintenanceToken | None, requested: MaintenanceToken | None) -> bool:
    """Identity, including absence, drives both runtime and generated models."""
    return current is not None and current is requested
