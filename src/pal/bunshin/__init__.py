"""Contract-driven Bunshin orchestration public surface."""

from pal.bunshin.capabilities import BunshinSnapshot, inspect_bunshin, register_with_core
from pal.bunshin.families import (
    BunshinCapabilityGroup,
    BunshinFamilyManifest,
    BunshinFamilyProvider,
    BunshinFamilyRegistry,
)
from pal.bunshin.ipc import BunshinManagerClient, BunshinManagerRpcError
from pal.bunshin.profiles import (
    BunshinProfile,
    BunshinProfileCapabilityProvider,
    BunshinProfileProvider,
    BunshinProfileRegistry,
)
from pal.bunshin.v2 import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    ArtifactRef,
    ContentAddressedArtifactStore,
    BunshinV2Repository,
)
from pal.bunshin.v2.service import BunshinV2WorkflowService

__all__ = [
    "ActionEnvelope",
    "AggregateSnapshot",
    "AggregateType",
    "ArtifactRef",
    "ContentAddressedArtifactStore",
    "BunshinCapabilityGroup",
    "BunshinFamilyManifest",
    "BunshinFamilyProvider",
    "BunshinFamilyRegistry",
    "BunshinManagerClient",
    "BunshinManagerRpcError",
    "BunshinProfile",
    "BunshinProfileCapabilityProvider",
    "BunshinProfileProvider",
    "BunshinProfileRegistry",
    "BunshinSnapshot",
    "BunshinV2Repository",
    "BunshinV2WorkflowService",
    "inspect_bunshin",
    "register_with_core",
]
