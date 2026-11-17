"""Background work keeps only its owning module generation alive."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock


@dataclass
class ModuleBackgroundLease:
    owner: ModuleBackgroundWork
    token: object

    def close(self) -> None:
        with self.owner._lock:
            self.owner._active.discard(self.token)


@dataclass
class ModuleBackgroundWork:
    _active: set[object] = field(default_factory=set, repr=False)
    _retired: bool = field(default=False, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def acquire(self) -> ModuleBackgroundLease | None:
        with self._lock:
            if self._retired:
                return None
            token = object()
            self._active.add(token)
            return ModuleBackgroundLease(self, token)

    def check_idle(self) -> None:
        with self._lock:
            if self._active:
                raise RuntimeError("Module has active background work; interrupt it or wait for completion before unloading")

    def retire(self) -> None:
        with self._lock:
            self.check_idle()
            self._retired = True

    def resume(self) -> None:
        with self._lock:
            self._retired = False
