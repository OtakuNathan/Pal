from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pal.execution.contracts import CapabilityDescriptor


@dataclass(frozen=True)
class IntrospectionCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntrospectionResult:
    status: str
    llm_text: str
    text: str = ""
    structured: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not str(self.llm_text or "").strip():
            raise ValueError("IntrospectionResult.llm_text must be non-empty")


@runtime_checkable
class IntrospectionPort(Protocol):
    module_id: str


@runtime_checkable
class LifecycleIntrospectionPort(IntrospectionPort, Protocol):
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        ...

    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        ...


def standard_descriptors(
    *,
    module_id: str,
    source: str,
    lifecycle_scope: str,
    detachable: bool,
    scope: str = "module",
    target_kind: str = "module",
    target_id: str | None = None,
    target_label: str | None = None,
    aliases: tuple[str, ...] = (),
) -> list[CapabilityDescriptor]:
    canonical_target_id = target_id or module_id
    canonical_target_label = target_label or module_id
    base_name = f"introspection_{scope}_{module_id}"
    return [
        CapabilityDescriptor(
            name=f"{base_name}_observe",
            family="introspection",
            description=f"Observe {scope}-level state for {module_id}",
            source=source,
            display_name=f"introspection_{scope}_{canonical_target_label}_observe",
            aliases=aliases
            + (
                f"{module_id}_introspection_observe",
                f"observe {module_id}",
                f"introspection {module_id} observe",
            ),
            target_kind=target_kind,
            target_id=canonical_target_id,
            target_label=canonical_target_label,
            metadata={"scope": scope, "action": "observe"},
            lifecycle_scope=lifecycle_scope,
            module_id=module_id,
            detachable=detachable,
        ),
        CapabilityDescriptor(
            name=f"{base_name}_configure",
            family="introspection",
            description=f"Configure {scope}-level state for {module_id}",
            source=source,
            display_name=f"introspection_{scope}_{canonical_target_label}_configure",
            aliases=aliases
            + (
                f"{module_id}_introspection_configure",
                f"configure {module_id}",
                f"introspection {module_id} configure",
            ),
            target_kind=target_kind,
            target_id=canonical_target_id,
            target_label=canonical_target_label,
            metadata={"scope": scope, "action": "configure"},
            lifecycle_scope=lifecycle_scope,
            module_id=module_id,
            detachable=detachable,
        ),
    ]
