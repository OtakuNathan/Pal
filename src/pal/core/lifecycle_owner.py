from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pal.shared import RuntimeStatus


@dataclass(frozen=True)
class ModuleLifecycleOwnerResult:
    status: str
    module_id: str
    owner_id: str
    fresh_instance: bool = False
    reload_modules: tuple[str, ...] = ()
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "module_id": self.module_id,
            "owner_id": self.owner_id,
            "fresh_instance": self.fresh_instance,
            "reload_modules": list(self.reload_modules),
        }
        if self.error:
            result["error"] = self.error
        result.update(self.payload)
        return result


class ModuleLifecycleOwner(Protocol):
    owner_id: str

    def owns_module(self, module_id: str) -> bool:
        ...

    def detach_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        ...

    def attach_module(self, module_id: str) -> ModuleLifecycleOwnerResult:
        ...


@dataclass
class ModuleLifecycleOwnerRegistry:
    owners: dict[str, ModuleLifecycleOwner] = field(default_factory=dict)
    module_owners: dict[str, str] = field(default_factory=dict)

    def register_owner(self, owner: ModuleLifecycleOwner, *, owner_id: str | None = None) -> None:
        resolved = str(owner_id or getattr(owner, "owner_id", "") or "").strip()
        if not resolved:
            raise ValueError("lifecycle owner_id is required")
        self.owners[resolved] = owner

    def bind_module(self, module_id: str, owner_id: str) -> None:
        module = str(module_id or "").strip()
        owner = str(owner_id or "").strip()
        if not module or not owner:
            return
        self.module_owners[module] = owner

    def unbind_module(self, module_id: str, *, owner_id: str | None = None) -> None:
        module = str(module_id or "").strip()
        if not module:
            return
        if owner_id is not None and self.module_owners.get(module) != owner_id:
            return
        self.module_owners.pop(module, None)

    def resolve(self, module_id: str) -> ModuleLifecycleOwner | None:
        module = str(module_id or "").strip()
        if not module:
            return None
        owner_id = self.module_owners.get(module)
        if owner_id:
            owner = self.owners.get(owner_id)
            if owner is not None:
                return owner
        for owner in self.owners.values():
            try:
                if owner.owns_module(module):
                    return owner
            except Exception:
                continue
        return None


def lifecycle_owner_not_found(module_id: str, owner_id: str) -> ModuleLifecycleOwnerResult:
    return ModuleLifecycleOwnerResult(
        status=RuntimeStatus.NOT_FOUND,
        module_id=module_id,
        owner_id=owner_id,
        error="module is not owned by this lifecycle owner",
    )
