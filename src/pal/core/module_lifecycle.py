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
        for descriptor_name in published:
            descriptor = self.context.execution_runtime.compiled_capability_index.records[descriptor_name]
            self.context.capability_registry.register(descriptor)
        handle.published_capabilities = published
        return published

    def withdraw_module_capabilities(self, module_id: str) -> list[str]:
        names = self.context.capability_registry.unregister_module(module_id)
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            self.context.execution_runtime.unmount_subtree(handle)
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            handle.published_capabilities = []
        return names

    def mount_module(self, handle):
        self.context.register_module(handle)
        self._restore_provider_refs(handle)
        self._restore_prompt_fragment_providers(handle)
        self._restore_event_sources(handle)
        self.publish_module_capabilities(handle.module_id)
        return handle

    def detach_module(self, module_id: str) -> str:
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
            for provider_id in list(handle.provider_refs):
                self.context.execution_runtime.unregister_provider_ref(provider_id)
                if self.context.execution_runtime.l3_plugin_registry.get(provider_id) is not None:
                    self.context.execution_runtime.l3_plugin_registry.plugins.pop(provider_id, None)
        elif handle.tier == MODULE_TIER_MANAGED_ESSENTIAL:
            handle.degraded = True
        self.state.detached_modules.add(module_id)
        return RuntimeStatus.OK

    def reattach_module(self, module_id: str) -> str:
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

    def _restore_prompt_fragment_providers(self, handle) -> None:
        for provider in handle.prompt_fragment_providers:
            self.context.prompt_fragment_registry.register(provider)
