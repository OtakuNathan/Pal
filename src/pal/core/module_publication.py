"""Generation-owned module publication and synchronous derived projections."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pal.core.module_registry import ModuleHandle


class ModulePublicationObserver(Protocol):
    def published(self, handle: ModuleHandle) -> None: ...
    def withdrawn(self, handle: ModuleHandle) -> None: ...


@dataclass(eq=False)
class _Compensation:
    observer_owner: ModuleHandle
    observer: ModulePublicationObserver
    handle: ModuleHandle
    previous: ModuleHandle | None = None
    withdrawn: bool = False

    def discard_restoration(self) -> None:
        if self.previous is not None:
            if self.withdrawn:
                # Restoration may have published part of the previous
                # projection before failing. Retire that projection too.
                self.handle = self.previous
                self.withdrawn = False
            self.previous = None

    def run(self) -> None:
        if not self.withdrawn:
            self.observer.withdrawn(self.handle)
            self.withdrawn = True
        if self.previous is not None:
            self.observer.published(self.previous)


def _compensate(steps: list[_Compensation], *, observer_owner: ModuleHandle | None = None) -> None:
    errors: list[BaseException] = []
    for step in tuple(steps):
        if observer_owner is not None and step.observer_owner is not observer_owner:
            continue
        try:
            step.run()
        except BaseException as error:
            errors.append(error)
        else:
            steps.remove(step)
    if not errors:
        return
    # Keep cancellation visible to the existing lifecycle owner while still
    # attempting independent projections and retaining every failed step.
    primary_error = next((item for item in errors if not isinstance(item, Exception)), errors[0])
    for other in errors:
        if other is not primary_error:
            primary_error.add_note(f"Additional projection cleanup failure: {type(other).__name__}: {other}")
    raise primary_error


@dataclass
class ModulePublications:
    handles: dict[str, ModuleHandle] = field(default_factory=dict)
    observers: dict[str, tuple[ModuleHandle, ModulePublicationObserver]] = field(default_factory=dict)
    _tokens: dict[str, object] = field(default_factory=dict, repr=False)
    _pending_publications: dict[str, tuple[ModuleHandle, list[_Compensation]]] = field(default_factory=dict, repr=False)
    _pending_subscriptions: dict[str, tuple[ModuleHandle, ModulePublicationObserver, list[_Compensation]]] = field(default_factory=dict, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def publish(self, handle: ModuleHandle) -> None:
        with self._lock:
            self._retry_publication(handle)
            previous = self.handles.get(handle.module_id)
            token = handle.mounted_subtree.admission if handle.mounted_subtree is not None else handle
            if previous is handle and self._tokens.get(handle.module_id) is token:
                return
            applied: list[_Compensation] = []
            try:
                for owner, observer in tuple(self.observers.values()):
                    applied.append(_Compensation(owner, observer, handle, previous))
                    observer.published(handle)
            except BaseException as error:
                self._pending_publications[handle.module_id] = (handle, list(reversed(applied)))
                try:
                    self._retry_publication(handle)
                except BaseException as cleanup_error:
                    error.add_note(f"Module projection cleanup remains pending: {type(cleanup_error).__name__}: {cleanup_error}")
                raise
            self.handles[handle.module_id] = handle
            self._tokens[handle.module_id] = token

    def withdraw(self, handle: ModuleHandle) -> None:
        with self._lock:
            pending = self._pending_publications.get(handle.module_id)
            if pending is not None:
                if pending[0] is handle:
                    if self.handles.get(handle.module_id) is handle:
                        # A failed refresh and its previous publication share
                        # one owner. Final withdrawal must not republish it.
                        for step in pending[1]:
                            step.discard_restoration()
                    self._retry_publication(handle)
                elif self.handles.get(handle.module_id) is handle:
                    raise RuntimeError(f"Module projection cleanup remains pending: {handle.module_id}")
                else:
                    return
            if self.handles.get(handle.module_id) is not handle:
                return
            for _, observer in tuple(self.observers.values()):
                observer.withdrawn(handle)
            self.handles.pop(handle.module_id)
            self._tokens.pop(handle.module_id, None)

    def subscribe(self, owner: ModuleHandle, observer: ModulePublicationObserver) -> None:
        with self._lock:
            self._retry_subscription(owner, observer)
            current = self.observers.get(owner.module_id)
            if current is not None:
                if current[0] is owner and current[1] is observer:
                    return
                raise ValueError(f"Module publication observer already registered: {owner.module_id}")
            applied: list[_Compensation] = []
            try:
                for handle in tuple(self.handles.values()):
                    applied.append(_Compensation(owner, observer, handle))
                    observer.published(handle)
            except BaseException as error:
                self._pending_subscriptions[owner.module_id] = (owner, observer, list(reversed(applied)))
                try:
                    self._retry_subscription(owner, observer)
                except BaseException as cleanup_error:
                    error.add_note(f"Module observer cleanup remains pending: {type(cleanup_error).__name__}: {cleanup_error}")
                raise
            self.observers[owner.module_id] = (owner, observer)

    def unsubscribe(self, owner: ModuleHandle) -> None:
        with self._lock:
            pending = self._pending_subscriptions.get(owner.module_id)
            if pending is not None and pending[0] is owner:
                self._retry_subscription(owner, pending[1])
            current = self.observers.get(owner.module_id)
            if current is None or current[0] is not owner:
                return
            # Retiring this observer needs only its own pending withdrawals,
            # never restoration or another observer's successful cleanup.
            for module_id, (_, steps) in tuple(self._pending_publications.items()):
                for step in steps:
                    if step.observer_owner is owner:
                        step.discard_restoration()
                _compensate(steps, observer_owner=owner)
                if not steps:
                    self._pending_publications.pop(module_id)
            for handle in tuple(self.handles.values()):
                current[1].withdrawn(handle)
            self.observers.pop(owner.module_id)

    def _retry_publication(self, handle: ModuleHandle) -> None:
        pending = self._pending_publications.get(handle.module_id)
        if pending is None:
            return
        if pending[0] is not handle:
            raise RuntimeError(f"Module projection cleanup belongs to another generation: {handle.module_id}")
        _compensate(pending[1])
        self._pending_publications.pop(handle.module_id)

    def _retry_subscription(self, owner: ModuleHandle, observer: ModulePublicationObserver) -> None:
        pending = self._pending_subscriptions.get(owner.module_id)
        if pending is None:
            return
        if pending[0] is not owner or pending[1] is not observer:
            raise RuntimeError(f"Module observer cleanup belongs to another generation: {owner.module_id}")
        _compensate(pending[2])
        self._pending_subscriptions.pop(owner.module_id)
