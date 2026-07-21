from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from pal.execution.tool_facade import (
    EffectReceipt,
    EmptyToolInput,
    OpaqueToolOutput,
    Tool,
    ToolExecutionSemantics,
    ToolGuidance,
)


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
    InputModel: type[BaseModel] | None = EmptyToolInput
    OutputModel: type[BaseModel] | None = OpaqueToolOutput
    guidance: ToolGuidance | None = None
    execution: ToolExecutionSemantics | None = None
    search_text: str = ""
    examples: tuple[dict[str, Any], ...] = ()
    mcp_input_schema: dict[str, Any] = field(default_factory=dict)
    mcp_output_schema: dict[str, Any] = field(default_factory=dict)
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
    llm_text: str
    text: str = ""
    structured: dict[str, Any] | None = None
    effect_receipt: EffectReceipt | None = None

    def __post_init__(self) -> None:
        if not str(self.llm_text or "").strip():
            raise ValueError("CapabilityResult.llm_text must be non-empty")


@dataclass(frozen=True)
class ToolCallBudget:
    max_output_chars: int | None = None
    max_output_tokens_estimate: int | None = None
    max_output_bytes: int | None = None
    max_result_spill_chars: int | None = None
    max_result_group_chars: int | None = None
    preview_chars: int | None = None
    artifact_bucket_id: str | None = None
    max_read_bytes: int | None = None
    max_lines_to_read: int | None = None
    max_stdout_chars: int | None = None
    timeout_ms: int | None = None


class Plugin(Protocol):
    def register(self, runtime: "ExecutionRuntimePort") -> None:
        ...


class ExecutionRuntimePort(Protocol):
    def register_provider_ref(self, provider_id: str, provider: Any) -> None:
        ...

    def unregister_provider_ref(self, provider_id: str) -> None:
        ...

    def register_tool(self, tool: Tool) -> None:
        ...

    def unregister_tool(self, name: str) -> None:
        ...

    def has_registered_capability(self, name: str) -> bool:
        ...

    def resolve_llm_tool_name(self, name: object) -> str:
        ...

    def execute_tool(self, call: Any, *, allow_tools: bool = True, budget: ToolCallBudget | None = None) -> Any:
        ...

    async def execute_tool_async(
        self,
        call: Any,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
    ) -> Any:
        ...

    def call_registered(self, call: CapabilityCall) -> CapabilityResult:
        ...

    def execute(self, call: CapabilityCall) -> CapabilityResult:
        ...

    async def execute_async(self, call: CapabilityCall) -> CapabilityResult:
        ...

    async def interrupt_turn(self, turn_id: str) -> None:
        ...
