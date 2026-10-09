"""Stable execution handle and transactional plugin-owned implementation replacement."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


@dataclass
class _PendingCleanup:
    extension: Any
    owner: Any
    candidate: Any
    restore: Callable[[], None] | None = None


class ExecutionSlot:
    """Keep references stable while a lifecycle-fenced plugin replaces execution."""
    def __init__(self, runtime):
        object.__setattr__(self, '_current', runtime)
        object.__setattr__(self, '_installed', None)
        object.__setattr__(self, '_pending_cleanup', None)

    def __getattr__(self, name):
        return getattr(self._current, name)

    def __setattr__(self, name, value):
        if name in {'_current', '_installed', '_pending_cleanup'}:
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
        if self._pending_cleanup is not None:
            raise RuntimeError('Execution extension cleanup is pending; retry uninstall first')
        if self._installed is not None:
            raise RuntimeError('An execution extension is already active')
        previous = self._current
        handle = context.module_registry.require('execution')
        saved = (handle.introspection_provider, handle.runtime_state_port, handle.mounted_subtree, handle.published_capabilities)
        candidate = extension.build_runtime(previous)
        # Framework state belongs to the logical execution session, not its implementation.
        staged = None
        published_candidate = False
        try:
            candidate.adopt_host_state(previous)
            provider = extension.build_provider(candidate)
            state_port = extension.build_state_port(candidate)
            staged = replace(handle, introspection_provider=provider, runtime_state_port=state_port,
                             mounted_subtree=None, published_capabilities=[])
            candidate.hydrate_module_handle(staged)
            published = candidate.mount_subtree(staged, retire_replaced=False)
            with candidate._registry_lock:
                self._publish(context, handle, candidate, staged, published)
                published_candidate = True
            extension.activate(context, owner_handle, candidate)
            for name, port in owner_handle.ports.items():
                context.port_registry[f'{owner_handle.module_id}:{name}'] = port
            self._installed = (extension, owner_handle, previous, saved, candidate)
        except BaseException as error:
            def restore_previous():
                with previous._registry_lock:
                    # Restore with fresh tokens; a captured failed candidate
                    # or retired original entry must never become live again.
                    subtree = saved[2]
                    restored = replace(handle, introspection_provider=saved[0], runtime_state_port=saved[1],
                                       mounted_subtree=(replace(subtree, bound_actions=list(subtree.bound_actions),
                                                                mounted=False) if subtree is not None else None),
                                       published_capabilities=[])
                    published = previous.mount_subtree(restored, retire_replaced=False)
                    self._publish(context, handle, previous, restored, published)

            # Retain ownership before attempting restoration: restoration can
            # itself fail, and close must wait until the candidate is replaced.
            self._pending_cleanup = _PendingCleanup(extension, owner_handle, candidate,
                restore_previous if published_candidate or self._current is candidate else None)
            with previous._registry_lock:
                if staged is not None:
                    # A staging failure must not retire the still-published
                    # original. A failed published candidate closes immediately,
                    # including when restoring the original cannot yet succeed.
                    self._retire(staged.mounted_subtree)
            try:
                self._close_pending_candidate()
            except (Exception, asyncio.CancelledError) as cleanup_error:
                error.add_note(f"Execution extension cleanup failed: {cleanup_error}")
            raise

    def check_detach(self, owner_handle):
        if self._installed is not None and self._installed[1] is owner_handle:
            extension, _, previous, _, candidate = self._installed
            if self._current is not previous:
                extension.check_detach(candidate)

    def uninstall(self, context, owner_handle):
        if self._pending_cleanup is not None and self._pending_cleanup.owner is owner_handle:
            self._close_pending_candidate()
            return
        if self._installed is None or self._installed[1] is not owner_handle:
            return
        extension, _, previous, saved, candidate = self._installed
        if self._current is not previous:
            extension.check_detach(candidate)
            handle = context.module_registry.require('execution')
            previous.adopt_host_state(candidate)
            provider, state_port, _, _ = saved
            staged = replace(handle, introspection_provider=provider, runtime_state_port=state_port,
                             mounted_subtree=None, published_capabilities=[])
            previous.hydrate_module_handle(staged)
            published = previous.mount_subtree(staged, retire_replaced=False)
            with previous._registry_lock:
                self._publish(context, handle, previous, staged, published)
        # Preserve cleanup ownership on failure. A retry closes this same
        # retired implementation without withdrawing the restored entries.
        extension.close(candidate)
        self._installed = None

    def _close_pending_candidate(self):
        pending = self._pending_cleanup
        if pending.restore is not None:
            pending.restore()
            pending.restore = None
        pending.extension.close(pending.candidate)
        self._pending_cleanup = None

    def _publish(self, context, handle, runtime, staged, published):
        # The shared registry lock makes withdrawal and the implementation
        # switch one admission boundary; admitted calls retain their own lease.
        self._retire(handle.mounted_subtree)
        self._current = runtime
        handle.introspection_provider = staged.introspection_provider
        handle.runtime_state_port = staged.runtime_state_port
        handle.mounted_subtree = staged.mounted_subtree
        handle.published_capabilities = published
        context.introspection_registry['execution'] = staged.introspection_provider

    @staticmethod
    def _retire(subtree):
        if subtree is not None:
            subtree.admission.live = False
            subtree.mounted = False


def execution_slot(runtime):
    return runtime if isinstance(runtime, ExecutionSlot) else ExecutionSlot(runtime)
