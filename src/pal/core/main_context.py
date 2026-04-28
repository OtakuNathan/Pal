from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pal.core.event_handler_registry import EventHandlerRegistry
from pal.core.event_source_registry import EventSourceRegistry
from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.core.turn_events import TurnEventBus
from pal.execution.capability_registry import CapabilityRegistry
from pal.execution.runtime import ExecutionRuntime
from pal.shared import IntrospectionPort


@dataclass
class MainContext:
    execution_runtime: ExecutionRuntime = field(default_factory=ExecutionRuntime)
    module_registry: ModuleRegistry = field(default_factory=ModuleRegistry)
    event_source_registry: EventSourceRegistry = field(default_factory=EventSourceRegistry)
    event_handler_registry: EventHandlerRegistry = field(default_factory=EventHandlerRegistry)
    prompt_fragment_registry: PromptFragmentRegistry = field(default_factory=PromptFragmentRegistry)
    turn_event_bus: TurnEventBus = field(default_factory=TurnEventBus)
    introspection_registry: dict[str, IntrospectionPort] = field(default_factory=dict)
    port_registry: dict[str, Any] = field(default_factory=dict)

    @property
    def capability_registry(self) -> CapabilityRegistry:
        return self.execution_runtime.capability_registry

    def register_module(self, handle: ModuleHandle) -> None:
        self.module_registry.register(handle)
        if handle.introspection_provider is not None:
            self.introspection_registry[handle.module_id] = handle.introspection_provider
            self.execution_runtime.hydrate_module_handle(handle)
        for port_name, port in handle.ports.items():
            self.port_registry[f"{handle.module_id}:{port_name}"] = port

    def require_port(self, key: str) -> Any:
        return self.port_registry[key]
