"""Stable execution handle and transactional plugin-owned implementation replacement."""
from __future__ import annotations

from dataclasses import fields, replace


class ExecutionSlot:
    """Keep references stable while a lifecycle-fenced plugin replaces execution."""
    def __init__(self, runtime):
        object.__setattr__(self, '_current', runtime)
        object.__setattr__(self, '_installed', None)

    def __getattr__(self, name):
        return getattr(self._current, name)

    def __setattr__(self, name, value):
        if name in {'_current', '_installed'}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._current, name, value)

    @property
    def implementation(self):
        return self._current

    def shutdown(self):
        return self._current.shutdown()

    async def shutdown_async(self):
        return await self._current.shutdown_async()

    def install(self, extension, context, owner_handle):
        from .runtime import ExecutionRuntime
        if self._installed is not None:
            raise RuntimeError('An execution extension is already active')
        previous = self._current
        handle = context.module_registry.require('execution')
        saved = (handle.introspection_provider, handle.runtime_state_port, handle.mounted_subtree, handle.published_capabilities)
        candidate = extension.build_runtime(previous)
        # Framework state belongs to the logical execution session, not its implementation.
        for item in fields(ExecutionRuntime):
            setattr(candidate, item.name, getattr(previous, item.name))
        try:
            provider = extension.build_provider(candidate)
            state_port = extension.build_state_port(candidate)
            staged = replace(handle, introspection_provider=provider, runtime_state_port=state_port,
                             mounted_subtree=None, published_capabilities=[])
            candidate.hydrate_module_handle(staged)
            published = candidate.mount_subtree(staged)
            self._current = candidate
            handle.introspection_provider = provider
            handle.runtime_state_port = state_port
            handle.mounted_subtree = staged.mounted_subtree
            handle.published_capabilities = published
            context.introspection_registry['execution'] = provider
            extension.activate(context, owner_handle, candidate)
            for name, port in owner_handle.ports.items():
                context.port_registry[f'{owner_handle.module_id}:{name}'] = port
            self._installed = (extension, owner_handle, previous, saved)
        except BaseException as error:
            self._current = previous
            (handle.introspection_provider, handle.runtime_state_port,
             handle.mounted_subtree, handle.published_capabilities) = saved
            context.introspection_registry['execution'] = saved[0]
            try:
                extension.close(candidate)
            except Exception as cleanup_error:
                error.add_note(f"Execution extension cleanup failed: {cleanup_error}")
            raise

    def check_detach(self, owner_handle):
        if self._installed is not None and self._installed[1] is owner_handle:
            self._installed[0].check_detach(self._current)

    def uninstall(self, context, owner_handle):
        from .runtime import ExecutionRuntime
        if self._installed is None or self._installed[1] is not owner_handle:
            return
        extension, _, previous, saved = self._installed
        extension.check_detach(self._current)
        handle = context.module_registry.require('execution')
        for item in fields(ExecutionRuntime):
            setattr(previous, item.name, getattr(self._current, item.name))
        provider, state_port, _, _ = saved
        staged = replace(handle, introspection_provider=provider, runtime_state_port=state_port,
                         mounted_subtree=None, published_capabilities=[])
        previous.hydrate_module_handle(staged)
        published = previous.mount_subtree(staged)
        extension.close(self._current)
        handle.introspection_provider = provider
        handle.runtime_state_port = state_port
        handle.mounted_subtree = staged.mounted_subtree
        handle.published_capabilities = published
        context.introspection_registry['execution'] = provider
        self._current = previous
        self._installed = None


def execution_slot(runtime):
    return runtime if isinstance(runtime, ExecutionSlot) else ExecutionSlot(runtime)
