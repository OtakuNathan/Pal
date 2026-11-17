"""Skill's projection of the currently published module generations."""
from dataclasses import dataclass

from pal.core.module_registry import ModuleHandle
from pal.skill.service import SkillService


@dataclass
class DeclaredSkillProjection:
    service: SkillService

    def __post_init__(self) -> None:
        self.service.repository.clear_declared_skills()

    def published(self, handle: ModuleHandle) -> None:
        self.service.register_declared_module(handle)

    def withdrawn(self, handle: ModuleHandle) -> None:
        self.service.unregister_declared_module(handle.module_id)
