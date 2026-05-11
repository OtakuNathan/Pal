from __future__ import annotations

from pal.core.module_registry import (
    MODULE_TIER_CORE_FOUNDATION,
    MODULE_TIER_DETACHABLE,
    MODULE_TIER_MANAGED_ESSENTIAL,
)
from pal.shared import IntrospectionCall, RuntimeStatus


class ModuleLifecycle:
    def __init__(self, context, state) -> None:
        self.context = context
        self.state = state

    def publish_module_capabilities(self, module_id: str) -> list[str]:
        handle = self.context.module_registry.require(module_id)
        if handle.introspection_provider is None:
            return []
        if handle.mounted_subtree is None or not handle.mounted_subtree.mounted:
            self.context.execution_runtime.hydrate_module_handle(handle)
        published = self.context.execution_runtime.mount_subtree(handle)
        try:
            for descriptor_name in published:
                descriptor = self.context.execution_runtime.compiled_capability_index.records[descriptor_name]
                self.context.capability_registry.register(descriptor)
            handle.published_capabilities = published
            self._register_behavior_declarations(handle)
            return published
        except Exception:
            self.context.capability_registry.unregister_module(module_id)
            self.context.execution_runtime.unmount_subtree(handle)
            self._unregister_behavior_declarations(module_id)
            handle.published_capabilities = []
            raise

    def withdraw_module_capabilities(self, module_id: str) -> list[str]:
        names = self.context.capability_registry.unregister_module(module_id)
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            self.context.execution_runtime.unmount_subtree(handle)
            self._unregister_behavior_declarations(handle.module_id)
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            handle.published_capabilities = []
        return names

    def mount_module(self, handle):
        self.context.register_module(handle)
        self._restore_provider_refs(handle)
        self._restore_prompt_fragment_providers(handle)
        self._restore_event_sources(handle)
        self._restore_event_handlers(handle)
        self.publish_module_capabilities(handle.module_id)
        return handle

    def detach_module(self, module_id: str) -> str:
        owner = self.context.lifecycle_owner_registry.resolve(module_id)
        if owner is not None:
            result = owner.detach_module(module_id)
            if result.status == RuntimeStatus.OK:
                self.state.detached_modules.add(module_id)
            return result.status
        handle = self.context.module_registry.require(module_id)
        if handle.tier == MODULE_TIER_CORE_FOUNDATION or not handle.supports_lifecycle_capabilities:
            return RuntimeStatus.FORBIDDEN
        provider = handle.introspection_provider
        if provider is not None and hasattr(provider, "detach"):
            provider.detach(IntrospectionCall(name=f"{module_id}.lifecycle.detach"))
        handle.mounted = False
        if handle.tier == MODULE_TIER_DETACHABLE:
            self.withdraw_module_capabilities(module_id)
            self.context.prompt_fragment_registry.unregister_module(module_id)
            self.context.event_source_registry.detach_module(module_id)
            self.context.event_handler_registry.detach_module(module_id)
            self.context.control_action_registry.unregister_module(module_id)
            for provider_id in list(handle.provider_refs):
                self.context.execution_runtime.unregister_provider_ref(provider_id)
                if self.context.execution_runtime.l3_plugin_registry.get(provider_id) is not None:
                    self.context.execution_runtime.l3_plugin_registry.plugins.pop(provider_id, None)
        elif handle.tier == MODULE_TIER_MANAGED_ESSENTIAL:
            handle.degraded = True
        self.state.detached_modules.add(module_id)
        return RuntimeStatus.OK

    def reattach_module(self, module_id: str) -> str:
        owner = self.context.lifecycle_owner_registry.resolve(module_id)
        if owner is not None:
            reloader = getattr(owner, "reload_module", None)
            result = reloader(module_id) if callable(reloader) else owner.attach_module(module_id)
            if result.status == RuntimeStatus.OK:
                self.state.detached_modules.discard(module_id)
            return result.status
        handle = self.context.module_registry.require(module_id)
        if handle.tier == MODULE_TIER_CORE_FOUNDATION or not handle.supports_lifecycle_capabilities:
            return RuntimeStatus.FORBIDDEN
        provider = handle.introspection_provider
        if provider is not None and hasattr(provider, "attach"):
            provider.attach(IntrospectionCall(name=f"{module_id}.lifecycle.attach"))
        handle.mounted = True
        handle.degraded = False
        if handle.tier == MODULE_TIER_DETACHABLE:
            self._restore_provider_refs(handle)
            self._restore_prompt_fragment_providers(handle)
            self._restore_event_sources(handle)
            self._restore_event_handlers(handle)
            self._restore_control_action_handlers(handle)
            self.publish_module_capabilities(module_id)
        self.state.detached_modules.discard(module_id)
        return RuntimeStatus.OK

    def _restore_provider_refs(self, handle) -> None:
        for provider_id in handle.provider_refs:
            provider = handle.ports.get(f"provider:{provider_id}")
            if provider is None:
                continue
            self.context.execution_runtime.register_provider_ref(provider_id, provider)
            if hasattr(provider, "provider_id") and self.context.execution_runtime.l3_plugin_registry.get(provider_id) is None:
                self.context.execution_runtime.l3_plugin_registry.register(provider)

    def _restore_event_sources(self, handle) -> None:
        for source in handle.event_sources:
            self.context.event_source_registry.attach(handle.module_id, source)

    def _restore_event_handlers(self, handle) -> None:
        for event_kind, handlers in handle.event_handlers.items():
            for handler in handlers:
                self.context.event_handler_registry.register(event_kind, handler, module_id=handle.module_id)

    def _restore_control_action_handlers(self, handle) -> None:
        for action_kind, handler in handle.control_action_handlers.items():
            self.context.control_action_registry.register(handle.module_id, action_kind, handler)

    def _restore_prompt_fragment_providers(self, handle) -> None:
        for provider in handle.prompt_fragment_providers:
            self.context.prompt_fragment_registry.register(provider)

    def _register_behavior_declarations(self, handle) -> None:
        skill = self.context.port_registry.get("skill:skill")
        skill_register = getattr(skill, "register_declared_module", None)
        if callable(skill_register):
            skill_register(handle)
        behavior = self.context.port_registry.get("behavior:behavior")
        register = getattr(behavior, "register_declared_module", None)
        if callable(register):
            register(handle)

    def _unregister_behavior_declarations(self, module_id: str) -> None:
        behavior = self.context.port_registry.get("behavior:behavior")
        unregister = getattr(behavior, "unregister_declared_module", None)
        if callable(unregister):
            unregister(module_id)
        skill = self.context.port_registry.get("skill:skill")
        skill_unregister = getattr(skill, "unregister_declared_module", None)
        if callable(skill_unregister):
            skill_unregister(module_id)
