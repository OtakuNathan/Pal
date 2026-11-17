from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pal.shared import IntrospectionPort, PromptFragmentProvider
from pal.shared.ports import PortKey
from pal.shared.timers import TimerScopePort
from pal.core.module_background import ModuleBackgroundWork
from pal.shared.registration import OwnerRelation, OwnershipEvent, owner_relation, reduce_ownership

if TYPE_CHECKING:
    from pal.core.module_publication import ModulePublicationObserver
    from pal.control.configuration_contracts import ConfigurationHandler
    from pal.control.contracts import ControlCommandSpec, ControlCommandLease, ControlInteractionSpec
    from pal.core.control_action_registry import ControlActionSpec, ControlObserver
    from pal.core.runtime_state import RuntimeStatePort
    from pal.shared import MountedSubtreeHandle
    from pal.memory.contracts import L3ProviderPort


MODULE_TIER_CORE_FOUNDATION = "core-foundation"
MODULE_TIER_MANAGED_ESSENTIAL = "managed-essential"
MODULE_TIER_DETACHABLE = "detachable"


@dataclass
class ModuleHandle:
    module_id: str
    tier: str
    mounted: bool = True
    detachable: bool = False
    degraded: bool = False
    introspection_provider: IntrospectionPort | None = None
    prompt_fragment_providers: list[PromptFragmentProvider] = field(default_factory=list)
    event_sources: list[Any] = field(default_factory=list)
    event_handlers: dict[str, list[Any]] = field(default_factory=dict)
    control_action_handlers: dict[str, ControlActionSpec] = field(default_factory=dict)
    control_status_provider: Callable[[], str] | None = None
    control_observers: dict[str, ControlObserver] = field(default_factory=dict)
    control_commands: tuple[ControlCommandSpec, ...] = ()
    control_interactions: tuple[ControlInteractionSpec, ...] = ()
    configuration_handlers: dict[str, ConfigurationHandler] = field(default_factory=dict)
    module_observer: ModulePublicationObserver | None = None
    timer_scope: TimerScopePort | None = None
    control_published: bool = field(default=False, init=False, repr=False)
    control_command_lease: ControlCommandLease | None = field(default=None, init=False, repr=False)
    provider_refs: list[str] = field(default_factory=list)
    l3_provider: L3ProviderPort | None = None
    ports: dict[str, Any] = field(default_factory=dict)
    port_contracts: tuple[PortKey[object], ...] = ()
    published_capabilities: list[str] = field(default_factory=list)
    mounted_subtree: "MountedSubtreeHandle | None" = None
    cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)
    shutdown_sync: Callable[[], None] | None = None
    shutdown_async: Callable[[], Awaitable[None]] | None = None
    quiesce_async: Callable[[], Awaitable[None]] | None = None
    execution_extension: Any | None = None
    channel_extension: Any | None = None
    runtime_state_port: "RuntimeStatePort | None" = None
    background_work: ModuleBackgroundWork = field(default_factory=ModuleBackgroundWork, init=False, repr=False)

@dataclass
class ModuleRegistry:
    modules: dict[str, ModuleHandle] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def register(self, handle: ModuleHandle) -> None:
        with self._lock:
            relation = owner_relation(self.modules.get(handle.module_id), handle)
            reduce_ownership(relation, OwnershipEvent.CLAIM)
            self.modules[handle.module_id] = handle

    def unregister(self, module_id: str, *, expected: ModuleHandle) -> ModuleHandle | None:
        with self._lock:
            relation = owner_relation(self.modules.get(module_id), expected)
            target = reduce_ownership(relation, OwnershipEvent.RELEASE)
            if relation is OwnerRelation.SELF and target is OwnerRelation.VACANT:
                return self.modules.pop(module_id)
            return None

    def get(self, module_id: str) -> ModuleHandle | None:
        return self.modules.get(module_id)

    def is_current(self, handle: ModuleHandle) -> bool:
        return self.modules.get(handle.module_id) is handle and handle.mounted

    def require(self, module_id: str) -> ModuleHandle:
        handle = self.get(module_id)
        if handle is None:
            raise KeyError(f"unknown module: {module_id}")
        return handle
