from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.repository import MinionV2Repository


class ResearchMode(StrEnum):
    NONE = "none"
    LOCAL_ONLY = "local_only"
    EXTERNAL_ALLOWED = "external_allowed"


@dataclass(frozen=True)
class ContractArtifactAccess:
    """Storage ports used by contract compilation and module-view projection."""

    artifacts: ContentAddressedArtifactStore
    repository: MinionV2Repository
