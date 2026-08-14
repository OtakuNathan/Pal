from __future__ import annotations

from dataclasses import dataclass

from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleMode


@dataclass(frozen=True)
class SemanticEffectRoute:
    """Declarative bridge from one durable effect to a role handler."""

    handler: str
    role: OrchestrationRole | None = None
    modes: frozenset[RoleMode] = frozenset()
    background: bool = False

    def __post_init__(self) -> None:
        if self.role is None and self.modes:
            raise ValueError("semantic route modes require a role")


def merge_effect_routes(
    *groups: dict[str, SemanticEffectRoute],
) -> dict[str, SemanticEffectRoute]:
    result: dict[str, SemanticEffectRoute] = {}
    for group in groups:
        overlap = set(result) & set(group)
        if overlap:
            raise ValueError("duplicate semantic effect routes: " + ", ".join(sorted(overlap)))
        result.update(group)
    return result
