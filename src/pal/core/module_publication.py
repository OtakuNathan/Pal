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


@dataclass
class ModulePublications:
    handles: dict[str, ModuleHandle] = field(default_factory=dict)
    observers: dict[str, tuple[ModuleHandle, ModulePublicationObserver]] = field(default_factory=dict)
    _tokens: dict[str, object] = field(default_factory=dict, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def publish(self, handle: ModuleHandle) -> None:
        with self._lock:
            previous = self.handles.get(handle.module_id)
            token = handle.mounted_subtree.admission if handle.mounted_subtree is not None else handle
            if previous is handle and self._tokens.get(handle.module_id) is token:
                return
            applied = []
            try:
                for _, observer in tuple(self.observers.values()):
                    applied.append(observer)
                    observer.published(handle)
            except BaseException:
                for observer in reversed(applied):
                    observer.withdrawn(handle)
                    if previous is not None:
                        observer.published(previous)
                raise
            self.handles[handle.module_id] = handle
            self._tokens[handle.module_id] = token

    def withdraw(self, handle: ModuleHandle) -> None:
        with self._lock:
            if self.handles.get(handle.module_id) is not handle:
                return
            for _, observer in tuple(self.observers.values()):
                observer.withdrawn(handle)
            self.handles.pop(handle.module_id)
            self._tokens.pop(handle.module_id, None)

    def subscribe(self, owner: ModuleHandle, observer: ModulePublicationObserver) -> None:
        with self._lock:
            current = self.observers.get(owner.module_id)
            if current is not None:
                if current[0] is owner and current[1] is observer:
                    return
                raise ValueError(f"Module publication observer already registered: {owner.module_id}")
            applied = []
            try:
                for handle in tuple(self.handles.values()):
                    applied.append(handle)
                    observer.published(handle)
            except BaseException:
                for handle in reversed(applied):
                    observer.withdrawn(handle)
                raise
            self.observers[owner.module_id] = (owner, observer)

    def unsubscribe(self, owner: ModuleHandle) -> None:
        with self._lock:
            current = self.observers.get(owner.module_id)
            if current is None or current[0] is not owner:
                return
            for handle in tuple(self.handles.values()):
                current[1].withdrawn(handle)
            self.observers.pop(owner.module_id)
