from __future__ import annotations

from pal.shared import RuntimeStatus


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
            handle.published_capabilities = published
            self._register_skill_declarations(handle)
            return published
        except Exception:
            self.context.execution_runtime.unmount_subtree(handle)
            self._unregister_skill_declarations(module_id)
            handle.published_capabilities = []
            raise

    def withdraw_module_capabilities(self, module_id: str) -> list[str]:
        names = list(self.context.capability_registry.by_module.get(module_id, ()))
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            self.context.execution_runtime.unmount_subtree(handle)
            self._unregister_skill_declarations(handle.module_id)
        handle = self.context.module_registry.get(module_id)
        if handle is not None:
            handle.published_capabilities = []
        return names

    def detach_module(self, module_id: str) -> str:
        owner = self.context.lifecycle_owner_registry.resolve(module_id)
        if owner is not None:
            result = owner.detach_module(module_id)
            if result.status == RuntimeStatus.OK:
                self.state.detached_modules.add(module_id)
            return result.status
        self.context.module_registry.require(module_id)
        return RuntimeStatus.FORBIDDEN

    def reattach_module(self, module_id: str) -> str:
        owner = self.context.lifecycle_owner_registry.resolve(module_id)
        if owner is not None:
            reloader = getattr(owner, "reload_module", None)
            result = reloader(module_id) if callable(reloader) else owner.attach_module(module_id)
            if result.status == RuntimeStatus.OK:
                self.state.detached_modules.discard(module_id)
            return result.status
        self.context.module_registry.require(module_id)
        return RuntimeStatus.FORBIDDEN

    def _register_skill_declarations(self, handle) -> None:
        skill = self.context.port_registry.get("skill:skill")
        skill_register = getattr(skill, "register_declared_module", None)
        if callable(skill_register):
            skill_register(handle)

    def _unregister_skill_declarations(self, module_id: str) -> None:
        skill = self.context.port_registry.get("skill:skill")
        skill_unregister = getattr(skill, "unregister_declared_module", None)
        if callable(skill_unregister):
            skill_unregister(module_id)
