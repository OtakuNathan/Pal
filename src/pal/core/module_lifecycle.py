from __future__ import annotations

from pal.shared import RuntimeStatus


class ModuleLifecycle:
    def __init__(self, context, state) -> None:
        self.context = context
        self.state = state

    def publish_module_capabilities(self, module_id: str) -> list[str]:
        return self.context.publish_module_capabilities(self.context.module_registry.require(module_id))

    def withdraw_module_capabilities(self, module_id: str) -> list[str]:
        handle = self.context.module_registry.get(module_id)
        return self.context.withdraw_module_capabilities(handle) if handle is not None else []

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
