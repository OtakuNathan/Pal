from __future__ import annotations

from typing import Protocol

from pal.foundation import PalV2Database
from pal.wizard import PalRegistration, WizardService


class RuntimeComposerPort(Protocol):
    def compose_runtime(
        self,
        *,
        wizard: WizardService,
        registration: PalRegistration,
        database: PalV2Database,
    ):
        ...
