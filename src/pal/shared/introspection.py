from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable



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
