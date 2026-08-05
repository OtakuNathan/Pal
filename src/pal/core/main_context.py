from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pal.core.event_handler_registry import EventHandlerRegistry
from pal.core.event_source_registry import EventSourceRegistry
from pal.core.control_action_registry import ControlActionHandlerRegistry
from pal.core.lifecycle_owner import ModuleLifecycleOwnerRegistry
from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.core.turn_events import TurnEventBus
from pal.execution.runtime import ExecutionRuntime
from pal.shared import IntrospectionPort


@dataclass
class MainContext:
    execution_runtime: ExecutionRuntime = field(default_factory=ExecutionRuntime)
    module_registry: ModuleRegistry = field(default_factory=ModuleRegistry)
    event_source_registry: EventSourceRegistry = field(default_factory=EventSourceRegistry)
    event_handler_registry: EventHandlerRegistry = field(default_factory=EventHandlerRegistry)
    control_action_registry: ControlActionHandlerRegistry = field(default_factory=ControlActionHandlerRegistry)
    prompt_fragment_registry: PromptFragmentRegistry = field(default_factory=PromptFragmentRegistry)
    lifecycle_owner_registry: ModuleLifecycleOwnerRegistry = field(default_factory=ModuleLifecycleOwnerRegistry)
    turn_event_bus: TurnEventBus = field(default_factory=TurnEventBus)
    introspection_registry: dict[str, IntrospectionPort] = field(default_factory=dict)
    port_registry: dict[str, Any] = field(default_factory=dict)

    @property
    def capability_registry(self):
        return self.execution_runtime.capability_registry

    def register_module(self, handle: ModuleHandle) -> None:
        module_id = str(handle.module_id or "").strip()
        if not module_id:
            raise ValueError("module_id is required")
        current = self.module_registry.get(module_id)
        if current is handle:
            return
        if current is not None:
            raise ValueError(f"module already registered: {module_id}")

        introspection = handle.introspection_provider
        existing_introspection = self.introspection_registry.get(module_id)
        if existing_introspection is not None and existing_introspection is not introspection:
            raise ValueError(f"module introspection provider already registered: {module_id}")
        for port_name, port in handle.ports.items():
            key = f"{module_id}:{port_name}"
            existing = self.port_registry.get(key)
            if existing is not None and existing is not port:
                raise ValueError(f"module port already registered: {key}")
        for action_kind, handler in handle.control_action_handlers.items():
            existing = self.control_action_registry.handlers.get(action_kind)
            if existing is not None and (existing[0] != module_id or existing[1] is not handler):
                raise ValueError(f"control action handler already registered: {action_kind}")

        # Hydration only prepares the candidate handle.  It must succeed before
        # any globally visible registry projection is committed.
        if introspection is not None:
            self.execution_runtime.hydrate_module_handle(handle)

        registered_ports: list[tuple[str, Any]] = []
        try:
            self.module_registry.register(handle)
            if introspection is not None:
                self.introspection_registry[module_id] = introspection
            for port_name, port in handle.ports.items():
                key = f"{module_id}:{port_name}"
                self.port_registry[key] = port
                registered_ports.append((key, port))
            for action_kind, handler in handle.control_action_handlers.items():
                self.control_action_registry.register(module_id, action_kind, handler)
        except Exception:
            self.control_action_registry.unregister_module(module_id)
            for key, port in registered_ports:
                if self.port_registry.get(key) is port:
                    self.port_registry.pop(key, None)
            if self.introspection_registry.get(module_id) is introspection:
                self.introspection_registry.pop(module_id, None)
            self.module_registry.unregister(module_id, expected=handle)
            raise

    def unregister_module(self, handle: ModuleHandle) -> bool:
        module_id = str(handle.module_id or "").strip()
        if self.module_registry.get(module_id) is not handle:
            return False
        self.control_action_registry.unregister_module(module_id)
        for port_name, port in handle.ports.items():
            key = f"{module_id}:{port_name}"
            if self.port_registry.get(key) is port:
                self.port_registry.pop(key, None)
        if self.introspection_registry.get(module_id) is handle.introspection_provider:
            self.introspection_registry.pop(module_id, None)
        self.module_registry.unregister(module_id, expected=handle)
        return True

    def require_port(self, key: str) -> Any:
        return self.port_registry[key]
