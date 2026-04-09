from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pal.foundation import PalV2Database


@dataclass(frozen=True)
class RuntimeLaunchSpec:
    db_path: Path
    runtime_root: Path
    pal_entrypoint: str


@dataclass(frozen=True)
class PalRegistration:
    display_name: str
    runtime: RuntimeLaunchSpec


@dataclass(frozen=True)
class ProvisionedRuntime:
    registration: PalRegistration
    database: "PalV2Database"


class SupervisorServicePort(Protocol):
    def register(self, registration: PalRegistration) -> None:
        ...

    def provision_runtime(
        self,
        *,
        display_name: str,
        runtime_root: Path,
        db_filename: str,
        pal_entrypoint: str,
    ) -> PalRegistration:
        ...

    def create_database(
        self,
        registration: PalRegistration,
    ) -> "PalV2Database":
        ...

    def seed_defaults(self, registration: PalRegistration) -> None:
        ...

    def provision_stub_runtime(self, runtime_root: Path) -> ProvisionedRuntime:
        ...
