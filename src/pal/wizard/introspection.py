from __future__ import annotations

from dataclasses import dataclass

from pal.wizard.runtime import WizardService


@dataclass(frozen=True)
class WizardSnapshot:
    registrations: int


def inspect_wizard(service: WizardService) -> WizardSnapshot:
    return WizardSnapshot(registrations=len(service.registrations))
