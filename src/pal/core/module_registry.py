from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pal.shared import IntrospectionPort, PromptFragmentProvider

if TYPE_CHECKING:
    from pal.shared import MountedSubtreeHandle


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
    control_action_handlers: dict[str, Any] = field(default_factory=dict)
    provider_refs: list[str] = field(default_factory=list)
    supports_lifecycle_capabilities: bool = False
    ports: dict[str, Any] = field(default_factory=dict)
    published_capabilities: list[str] = field(default_factory=list)
    mounted_subtree: "MountedSubtreeHandle | None" = None
    cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)
    shutdown_sync: Callable[[], None] | None = None
    shutdown_async: Callable[[], Awaitable[None]] | None = None


@dataclass
class ModuleRegistry:
    modules: dict[str, ModuleHandle] = field(default_factory=dict)

    def register(self, handle: ModuleHandle) -> None:
        self.modules[handle.module_id] = handle

    def get(self, module_id: str) -> ModuleHandle | None:
        return self.modules.get(module_id)

    def require(self, module_id: str) -> ModuleHandle:
        handle = self.get(module_id)
        if handle is None:
            raise KeyError(f"unknown module: {module_id}")
        return handle
