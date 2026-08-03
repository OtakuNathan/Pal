from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from pal.execution.tool_facade import (
    EffectReceipt,
    ToolExecutionSemantics,
    ToolGuidance,
    model_validation_schema,
)


@dataclass(frozen=True)
class CapabilityDescriptor:
    name: str
    family: str
    description: str
    source: str
    canonical_path: str = ""
    display_name: str | None = None
    # Exactly one LLM-facing alias is required when the descriptor is compiled.
    # canonical_path remains manager-only addressing metadata.
    aliases: tuple[str, ...] = ()
    target_kind: str = ""
    target_id: str = ""
    target_label: str = ""
    InputModel: type[BaseModel] | None = None
    OutputModel: type[BaseModel] | None = None
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

    def __post_init__(self) -> None:
        declared = tuple(str(value or "").strip() for value in self.aliases)
        if len(declared) != 1 or not declared[0]:
            raise ValueError(
                f"capability descriptor {self.canonical_path or self.name!r} must declare exactly one non-empty alias"
            )
        object.__setattr__(self, "aliases", declared)
        is_mcp = isinstance(self.metadata.get("mcp"), dict)
        if is_mcp:
            if self.InputModel is not None or self.OutputModel is not None:
                raise TypeError(
                    f"MCP capability {self.canonical_path or self.name!r} "
                    "must retain its server JSON Schema instead of Pydantic models"
                )
            return
        for label, model in (
            ("InputModel", self.InputModel),
            ("OutputModel", self.OutputModel),
        ):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise TypeError(
                    f"internal capability {self.canonical_path or self.name!r} "
                    f"requires a Pydantic {label}"
                )
            if model.model_config.get("strict") is not True:
                raise ValueError(f"{model.__name__} must set strict=True")
            if model.model_config.get("extra") != "forbid":
                raise ValueError(f"{model.__name__} must set extra='forbid'")
        input_schema = model_validation_schema(self.InputModel)
        if input_schema.get("properties") and not self.examples:
            raise ValueError(
                f"non-empty InputModel for {declared[0]!r} requires at least one example"
            )
        for example in self.examples:
            self.InputModel.model_validate(example, strict=True)


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
    context_delivery: dict[str, Any] | None = None

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

    def has_registered_capability(self, name: str) -> bool:
        ...

    def resolve_capability_address(self, name: object) -> str:
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

    def commit_tool_delivery(
        self,
        *,
        turn_id: str | None,
        context_delivery: dict[str, Any] | None,
    ) -> Any:
        ...
