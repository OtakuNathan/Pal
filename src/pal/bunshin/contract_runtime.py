from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.repository import BunshinRepository


class ResearchMode(StrEnum):
    NONE = "none"
    LOCAL_ONLY = "local_only"
    EXTERNAL_ALLOWED = "external_allowed"


@dataclass(frozen=True)
class ContractArtifactAccess:
    """Storage ports used by contract compilation and module-view projection."""

    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
