from __future__ import annotations

from typing import Protocol

from pal.foundation import PalV2Database
from pal.supervisor import PalRegistration, SupervisorService


class RuntimeComposerPort(Protocol):
    def compose_runtime(
        self,
        *,
        supervisor: SupervisorService,
        registration: PalRegistration,
        database: PalV2Database,
    ):
        ...
