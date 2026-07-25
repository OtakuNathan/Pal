from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


TASK_PROFILE_BINDING = "$task_profile"


class OrchestrationRole(StrEnum):
    ARCHITECT = "architect"
    REVIEWER = "reviewer"
    IMPLEMENTATION = "implementation"
    VERIFIER = "verifier"


class RoleMode(StrEnum):
    AUTHOR = "author"
    REVISION = "revision"
    ARCHITECTURE = "architecture"
    STANDALONE = "standalone"
    PRODUCE = "produce"
    REPAIR = "repair"
    MODULE = "module"
    SYSTEM = "system"


ROLE_MODES: Mapping[OrchestrationRole, frozenset[RoleMode]] = {
    OrchestrationRole.ARCHITECT: frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
    OrchestrationRole.REVIEWER: frozenset({RoleMode.ARCHITECTURE, RoleMode.STANDALONE}),
    OrchestrationRole.IMPLEMENTATION: frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
    OrchestrationRole.VERIFIER: frozenset({RoleMode.MODULE, RoleMode.SYSTEM}),
}


@dataclass(frozen=True)
class RoleActivation:
    role: OrchestrationRole
    mode: RoleMode

    def __post_init__(self) -> None:
        if self.mode not in ROLE_MODES[self.role]:
            raise ValueError(
                f"invalid orchestration role activation: {self.role.value}/{self.mode.value}"
            )

    @classmethod
    def from_values(
        cls,
        role: OrchestrationRole | str,
        mode: RoleMode | str,
    ) -> "RoleActivation":
        return cls(OrchestrationRole(str(role)), RoleMode(str(mode)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RoleActivation":
        return cls.from_values(str(value.get("role") or ""), str(value.get("mode") or ""))

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role.value, "mode": self.mode.value}


REQUIRED_ORCHESTRATION_ROLES = frozenset(role.value for role in OrchestrationRole)


def validate_role_bindings(value: Mapping[str, str]) -> dict[str, str]:
    bindings = {
        str(role).strip(): str(profile).strip().replace("/", ".")
        for role, profile in dict(value or {}).items()
        if str(role).strip() or str(profile).strip()
    }
    missing = sorted(REQUIRED_ORCHESTRATION_ROLES - set(bindings))
    extra = sorted(set(bindings) - REQUIRED_ORCHESTRATION_ROLES)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise ValueError("family role_bindings must define exactly four roles: " + "; ".join(details))
    empty = sorted(role for role, profile in bindings.items() if not profile)
    if empty:
        raise ValueError("family role_bindings have empty profile selectors: " + ", ".join(empty))
    return {role: bindings[role] for role in sorted(bindings)}
