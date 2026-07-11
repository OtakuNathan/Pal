"""Contract-driven Minion orchestration public surface."""

from pal.minion.capabilities import MinionSnapshot, inspect_minion, register_with_core
from pal.minion.families import (
    MinionCapabilityGroup,
    MinionFamilyManifest,
    MinionFamilyProvider,
    MinionFamilyRegistry,
)
from pal.minion.ipc import MinionManagerClient, MinionManagerRpcError
from pal.minion.profiles import (
    MinionProfile,
    MinionProfileCapabilityProvider,
    MinionProfileProvider,
    MinionProfileRegistry,
)
from pal.minion.v2 import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    ArtifactRef,
    ContentAddressedArtifactStore,
    MinionV2Repository,
)
from pal.minion.v2.service import MinionV2WorkflowService

__all__ = [
    "ActionEnvelope",
    "AggregateSnapshot",
    "AggregateType",
    "ArtifactRef",
    "ContentAddressedArtifactStore",
    "MinionCapabilityGroup",
    "MinionFamilyManifest",
    "MinionFamilyProvider",
    "MinionFamilyRegistry",
    "MinionManagerClient",
    "MinionManagerRpcError",
    "MinionProfile",
    "MinionProfileCapabilityProvider",
    "MinionProfileProvider",
    "MinionProfileRegistry",
    "MinionSnapshot",
    "MinionV2Repository",
    "MinionV2WorkflowService",
    "inspect_minion",
    "register_with_core",
]
