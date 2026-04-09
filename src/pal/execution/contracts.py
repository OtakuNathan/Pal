from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CapabilityDescriptor:
    name: str
    family: str
    description: str
    source: str
    canonical_path: str = ""
    display_name: str | None = None
    aliases: tuple[str, ...] = ()
    target_kind: str = ""
    target_id: str = ""
    target_label: str = ""
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    lifecycle_scope: str = "runtime"
    module_id: str = ""
    detachable: bool = False


@dataclass(frozen=True)
class CapabilityCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityResult:
    status: str
    text: str = ""
    structured: dict[str, Any] | None = None


CapabilityCallable = Callable[[CapabilityCall], CapabilityResult]


@dataclass(frozen=True)
class RegisteredCapability:
    descriptor: CapabilityDescriptor
    callable: CapabilityCallable


class Tool(Protocol):
    name: str
    description: str
    args_schema: dict[str, Any]
    result_schema: dict[str, Any]
    display_name: str
    family: str
    tags: tuple[str, ...]
    keywords: tuple[str, ...]

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        ...


class Plugin(Protocol):
    def register(self, runtime: "ExecutionRuntimePort") -> None:
        ...


class ExecutionRuntimePort(Protocol):
    def register_capability(self, descriptor: CapabilityDescriptor, callable: CapabilityCallable) -> None:
        ...

    def unregister_capability(self, name: str) -> None:
        ...

    def register_provider_ref(self, provider_id: str, provider: Any) -> None:
        ...

    def unregister_provider_ref(self, provider_id: str) -> None:
        ...

    def register_tool(self, tool: Tool) -> None:
        ...

    def execute_tool(self, call: Any, *, allow_tools: bool = True) -> Any:
        ...

    async def execute_tool_async(self, call: Any, *, allow_tools: bool = True) -> Any:
        ...

    def call_registered(self, call: CapabilityCall) -> CapabilityResult:
        ...

    def execute(self, call: CapabilityCall) -> CapabilityResult:
        ...

    async def execute_async(self, call: CapabilityCall) -> CapabilityResult:
        ...
