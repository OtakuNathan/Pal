from __future__ import annotations

from dataclasses import dataclass, field

from pal.execution.contracts import CapabilityDescriptor


@dataclass
class CapabilityRegistry:
    descriptors: dict[str, CapabilityDescriptor] = field(default_factory=dict)
    by_module: dict[str, list[str]] = field(default_factory=dict)

    def register(self, descriptor: CapabilityDescriptor) -> None:
        self.descriptors[descriptor.name] = descriptor
        bucket = self.by_module.setdefault(descriptor.module_id, [])
        if descriptor.name not in bucket:
            bucket.append(descriptor.name)

    def unregister(self, name: str) -> None:
        descriptor = self.descriptors.pop(name, None)
        if descriptor is None:
            return
        bucket = self.by_module.get(descriptor.module_id, [])
        if name in bucket:
            bucket.remove(name)

    def unregister_module(self, module_id: str) -> list[str]:
        names = list(self.by_module.pop(module_id, []))
        for name in names:
            self.descriptors.pop(name, None)
        return names
