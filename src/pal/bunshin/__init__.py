"""Contract-driven Bunshin orchestration public surface."""

from typing import TYPE_CHECKING, Any

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
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
from pal.bunshin.repository import BunshinRepository

if TYPE_CHECKING:
    from pal.bunshin.capabilities import BunshinSnapshot, inspect_bunshin, register_with_core
    from pal.bunshin.service import BunshinWorkflowService

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
    "BunshinRepository",
    "BunshinWorkflowService",
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


def __getattr__(name: str) -> Any:
    """Load host composition only when requested through the public package."""
    if name == "BunshinWorkflowService":
        from pal.bunshin.service import BunshinWorkflowService

        return BunshinWorkflowService
    if name in {"BunshinSnapshot", "inspect_bunshin", "register_with_core"}:
        from pal.bunshin.capabilities import BunshinSnapshot, inspect_bunshin, register_with_core

        return {
            "BunshinSnapshot": BunshinSnapshot,
            "inspect_bunshin": inspect_bunshin,
            "register_with_core": register_with_core,
        }[name]
    raise AttributeError(name)
