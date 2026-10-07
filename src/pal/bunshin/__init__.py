"""Contract-driven Bunshin orchestration public surface."""

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.capabilities import BunshinSnapshot, inspect_bunshin, register_with_core
from pal.bunshin.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    DomainEvent,
    EffectDraft,
    TaskState,
    TransitionError,
    TransitionOutcome,
)
from pal.bunshin.engine import TransitionEngine
from pal.bunshin.families import (
    BunshinCapabilityGroup,
    BunshinFamilyManifest,
    BunshinFamilyProvider,
    BunshinFamilyRegistry,
)
from pal.bunshin.ipc import BunshinManagerClient, BunshinManagerRpcError
from pal.bunshin.machines import build_default_transition_engine
from pal.bunshin.profiles import (
    BunshinProfile,
    BunshinProfileCapabilityProvider,
    BunshinProfileProvider,
    BunshinProfileRegistry,
)
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.service import BunshinV2WorkflowService

__all__ = [
    "ActionEnvelope",
    "AggregateSnapshot",
    "AggregateType",
    "ArtifactRef",
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
    "ContentAddressedArtifactStore",
    "DomainEvent",
    "EffectDraft",
    "TaskState",
    "TransitionEngine",
    "TransitionError",
    "TransitionOutcome",
    "build_default_transition_engine",
    "inspect_bunshin",
    "register_with_core",
]
