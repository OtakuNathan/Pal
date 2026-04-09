from __future__ import annotations

from dataclasses import dataclass

from pal.supervisor.runtime import SupervisorService


@dataclass(frozen=True)
class SupervisorSnapshot:
    registrations: int


def inspect_supervisor(service: SupervisorService) -> SupervisorSnapshot:
    return SupervisorSnapshot(registrations=len(service.registrations))
