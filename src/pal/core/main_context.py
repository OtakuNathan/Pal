from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, TypeVar, overload

from pal.core.event_handler_registry import EventHandlerRegistry
from pal.core.event_source_registry import EventSourceRegistry
from pal.core.control_action_registry import ControlActionHandlerRegistry
from pal.core.lifecycle_owner import ModuleLifecycleOwnerRegistry
from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.module_publication import ModulePublications
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.core.turn_events import TurnEventBus
from pal.execution.contracts import ExecutionRuntimePort
from pal.memory.contracts import MEMORY
from pal.shared import IntrospectionPort
from pal.shared.ports import PortKey, PortRegistry

T = TypeVar("T")

if TYPE_CHECKING:
    from pal.control.configuration_contracts import ConfigurationHandler
    from pal.control.contracts import ControlPlanePort


@dataclass
class MainContext:
    execution_runtime: ExecutionRuntimePort
    module_registry: ModuleRegistry = field(default_factory=ModuleRegistry)
    module_publications: ModulePublications = field(default_factory=ModulePublications)
    event_source_registry: EventSourceRegistry = field(default_factory=EventSourceRegistry)
    event_handler_registry: EventHandlerRegistry = field(default_factory=EventHandlerRegistry)
    control_action_registry: ControlActionHandlerRegistry = field(init=False)
    prompt_fragment_registry: PromptFragmentRegistry = field(default_factory=PromptFragmentRegistry)
    lifecycle_owner_registry: ModuleLifecycleOwnerRegistry = field(default_factory=ModuleLifecycleOwnerRegistry)
    turn_event_bus: TurnEventBus = field(default_factory=TurnEventBus)
    introspection_registry: dict[str, IntrospectionPort] = field(default_factory=dict)
    port_registry: PortRegistry = field(default_factory=PortRegistry)
    configuration_operations: dict[str, tuple[str, ConfigurationHandler]] = field(default_factory=dict)
    _command_router: ControlPlanePort | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        from pal.execution.extensions import execution_slot
        self.execution_runtime = execution_slot(self.execution_runtime)
        self.control_action_registry = ControlActionHandlerRegistry(self.execution_runtime.lifecycle_gate)

    @property
    def core_event_bus(self) -> TurnEventBus:
        return self.turn_event_bus

    @property
    def capability_registry(self):
        return self.execution_runtime.capability_registry

    def register_module(self, handle: ModuleHandle, *, publish_control: bool = True) -> None:
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
        for contract in handle.port_contracts:
            self.port_registry.declare(contract)
        for port_name, port in handle.ports.items():
            key = f"{module_id}:{port_name}"
            self.port_registry.validate(key, port)
            existing = self.port_registry.get(key)
            if existing is not None and existing is not port:
                raise ValueError(f"module port already registered: {key}")
        for action_kind, handler in handle.control_action_handlers.items():
            existing = self.control_action_registry.handlers.get(action_kind)
            if existing is not None and (existing[0] != module_id or existing[1] is not handler):
                raise ValueError(f"control action handler already registered: {action_kind}")

        for name in handle.configuration_handlers:
            existing_operation = self.configuration_operations.get(name)
            if existing_operation is not None:
                raise ValueError(f"configuration operation already registered: {name}")

        for name in handle.provider_refs:
            provider = handle.ports.get(f"provider:{name}")
            if provider is None:
                raise ValueError(f"Provider reference has no declared binding: {name}")
            self.execution_runtime.validate_provider_ref(name, provider, owner_id=module_id)
        if handle.l3_provider is not None:
            self.require_port(MEMORY).validate_l3_provider(handle.l3_provider, module_id=module_id)

        # Hydration only prepares the candidate handle.  It must succeed before
        # any globally visible registry projection is committed.
        if introspection is not None:
            self.execution_runtime.hydrate_module_handle(handle)

        if handle.timer_scope is not None:
            handle.timer_scope.bind_owner(lambda: self.module_registry.is_current(handle))
        # Claim the name before entering rollback for shared projections. A
        # rejected claim has never acquired authority to withdraw those entries.
        self.module_registry.register(handle)
        try:
            if introspection is not None:
                self.introspection_registry[module_id] = introspection
            bindings = {f"{module_id}:{name}": port for name, port in handle.ports.items()}
            self.port_registry.publish(bindings)
            if handle.l3_provider is not None:
                self.require_port(MEMORY).register_l3_provider(handle.l3_provider)
            if publish_control:
                for name in handle.provider_refs:
                    self.execution_runtime.register_provider_ref(
                        name, handle.ports[f"provider:{name}"], owner_id=module_id)
                self.publish_module_control(handle)
                self.publish_module_observer(handle)
        except BaseException as error:
            # Rejected registration cannot admit callbacks while its derived
            # projections are still being cleaned. Keep the exact name owner
            # until unregister succeeds; callers retry unregister_module(handle).
            handle.mounted = False
            try:
                self.execution_runtime.revoke_subtree_admission(handle)
                self.withdraw_module_control(handle)
                self.unregister_module(handle)
            except BaseException as cleanup_error:
                error.add_note(f"Module registration cleanup remains pending: {type(cleanup_error).__name__}: {cleanup_error}")
            raise

    def unregister_module(self, handle: ModuleHandle) -> bool:
        module_id = str(handle.module_id or "").strip()
        if self.module_registry.get(module_id) is not handle:
            return False
        handle.background_work.retire()
        handle.mounted = False
        self.execution_runtime.revoke_subtree_admission(handle)
        self.withdraw_module_control(handle)
        for name in handle.provider_refs:
            self.execution_runtime.unregister_provider_ref(name, expected=handle.ports[f"provider:{name}"])
        self.event_handler_registry.detach_module(module_id, expected_owner=handle)
        if handle.l3_provider is not None:
            self.require_port(MEMORY).unregister_l3_provider(handle.l3_provider)
        self.module_publications.unsubscribe(handle)
        self.module_publications.withdraw(handle)
        if self._command_router is not None and handle.ports.get("control") is self._command_router:
            for registered in self.module_registry.modules.values():
                if registered.control_command_lease is not None:
                    registered.control_command_lease.close()
                    registered.control_command_lease = None
            self._command_router = None
        self.port_registry.withdraw({f"{module_id}:{name}": port for name, port in handle.ports.items()})
        if self.introspection_registry.get(module_id) is handle.introspection_provider:
            self.introspection_registry.pop(module_id, None)
        self.module_registry.unregister(module_id, expected=handle)
        return True

    def publish_module_observer(self, handle: ModuleHandle) -> None:
        if handle.module_observer is not None:
            self.module_publications.subscribe(handle, handle.module_observer)

    def publish_module_capabilities(self, handle: ModuleHandle, *, notify: bool = True) -> list[str]:
        if handle.introspection_provider is None:
            return []
        runtime = self.execution_runtime
        if handle.mounted_subtree is None or not handle.mounted_subtree.mounted:
            runtime.hydrate_module_handle(handle)
        try:
            handle.published_capabilities = runtime.mount_subtree(handle)
            if notify:
                self.module_publications.publish(handle)
        except BaseException:
            runtime.unmount_subtree(handle)
            self.module_publications.withdraw(handle)
            handle.published_capabilities = []
            raise
        return handle.published_capabilities

    def withdraw_module_capabilities(self, handle: ModuleHandle) -> list[str]:
        names = self.execution_runtime.unmount_subtree(handle)
        self.module_publications.withdraw(handle)
        handle.published_capabilities = []
        return names

    def refresh_module_capabilities(self, handle: ModuleHandle) -> list[str]:
        previous = handle.mounted_subtree
        if previous is None or not previous.mounted:
            return self.publish_module_capabilities(handle)
        runtime = self.execution_runtime
        staged = replace(handle, mounted_subtree=None, published_capabilities=[])
        runtime.hydrate_module_handle(staged)
        published = runtime.mount_subtree(staged)
        previous.mounted = False
        handle.mounted_subtree = staged.mounted_subtree
        handle.published_capabilities = published
        try:
            self.module_publications.publish(handle)
        except BaseException as error:
            # A rejected candidate must never become callable again. Restore
            # the old compiled surface with fresh admission tokens, preserving
            # registry changes made to other modules in the meantime.
            runtime.revoke_subtree_admission(staged)
            handle.mounted_subtree = replace(previous, bound_actions=list(previous.bound_actions), mounted=False)
            handle.published_capabilities = []
            try:
                handle.published_capabilities = runtime.mount_subtree(handle)
                self.module_publications.publish(handle)
            except BaseException as rollback_error:
                error.add_note(f"Module capability restoration failed: {rollback_error}")
            raise
        return published

    def install_command_router(self, router: ControlPlanePort) -> None:
        if self._command_router is router:
            return
        if self._command_router is not None:
            raise ValueError("a control command router is already installed")
        installed: list[ModuleHandle] = []
        try:
            for handle in self.module_registry.modules.values():
                if handle.control_published and (handle.control_commands or handle.control_interactions):
                    handle.control_command_lease = router.install_commands(
                        handle.module_id, handle.control_commands, interactions=handle.control_interactions)
                    installed.append(handle)
        except Exception:
            for handle in reversed(installed):
                handle.control_command_lease.close()
                handle.control_command_lease = None
            raise
        self._command_router = router

    def publish_module_control(self, handle: ModuleHandle) -> None:
        if handle.control_published:
            return
        if self._command_router is not None and (handle.control_commands or handle.control_interactions):
            handle.control_command_lease = self._command_router.install_commands(
                handle.module_id, handle.control_commands, interactions=handle.control_interactions)
        try:
            for kind, spec in handle.control_action_handlers.items():
                self.control_action_registry.register(handle.module_id, kind, spec)
            for name, operation in handle.configuration_handlers.items():
                if name in self.configuration_operations:
                    raise ValueError(f"configuration operation already registered: {name}")
                self.configuration_operations[name] = (handle.module_id, operation)
            if handle.control_status_provider is not None:
                self.control_action_registry.status_providers[handle.module_id] = handle.control_status_provider
            for topic, callback in handle.control_observers.items():
                self.control_action_registry.observers.setdefault(topic, {})[handle.module_id] = callback
        except Exception:
            self.withdraw_module_control(handle)
            raise
        handle.control_published = True

    def withdraw_module_control(self, handle: ModuleHandle) -> None:
        if handle.timer_scope is not None:
            handle.timer_scope.close()
        for name, operation in handle.configuration_handlers.items():
            current = self.configuration_operations.get(name)
            if current is not None and current[0] == handle.module_id and current[1] is operation:
                self.configuration_operations.pop(name)
        if handle.control_command_lease is not None:
            handle.control_command_lease.close()
            handle.control_command_lease = None
        self.control_action_registry.unregister_module(
            handle.module_id, expected=handle.control_action_handlers,
            status_provider=handle.control_status_provider, observers=handle.control_observers)
        handle.control_published = False

    @overload
    def require_port(self, key: PortKey[T]) -> T: ...

    @overload
    def require_port(self, key: str) -> Any: ...

    def require_port(self, key: PortKey[T] | str) -> T | Any:
        if isinstance(key, PortKey):
            return self.port_registry.require(key)
        return self.port_registry[key]
