from __future__ import annotations

from pal.shared.result_snapshot import ResultSnapshotRef

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel

from pal.execution.tool_facade import (
    EffectReceipt,
    ToolExecutionSemantics,
    ToolGuidance,
    model_validation_schema,
)
from pal.shared.tool_protocol import ToolAffordance, ToolCallIR, ToolContextMessageIR, ToolExecutionResult

if TYPE_CHECKING:
    from pal.foundation.lifecycle_gate import WriterPreferredRWGate
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from pal.execution.logical_sessions import LogicalExecutionSessions
    from pal.execution.result_snapshots import ResultSnapshotStore
    from pal.execution.session_state import LogicalExecutionContext, LogicalExecutionStateBackend
    from pal.execution.tool_registry import ToolRegistryGeneration
    from pal.memory.contracts import MemoryServicePort


@dataclass(frozen=True)
class CapabilityDescriptor:
    name: str
    family: str
    source: str
    guidance: ToolGuidance
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
    execution: ToolExecutionSemantics | None = None
    examples: tuple[dict[str, Any], ...] = ()
    mcp_input_schema: dict[str, Any] = field(default_factory=dict)
    mcp_output_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    lifecycle_scope: str = "runtime"
    module_id: str = ""
    detachable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.guidance, ToolGuidance):
            raise TypeError(
                f"capability descriptor {self.canonical_path or self.name!r} requires ToolGuidance"
            )
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
            if (
                not model.__pydantic_root_model__
                and model.model_config.get("extra") != "forbid"
            ):
                raise ValueError(f"{model.__name__} must set extra='forbid'")
        input_schema = model_validation_schema(self.InputModel)
        if input_schema.get("properties") and not self.examples:
            raise ValueError(
                f"non-empty InputModel for {declared[0]!r} requires at least one example"
            )
        input_model = cast(type[BaseModel], self.InputModel)
        for example in self.examples:
            input_model.model_validate(example, strict=True)


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
    # Result-specific guidance (task package v2 §5.1): business owners emit
    # affordances only when this result's facts justify an action.
    affordances: tuple[ToolAffordance, ...] = ()
    recovery_hint: str = ""
    context_delivery: dict[str, Any] | None = None
    snapshot_refs: tuple[ResultSnapshotRef, ...] = ()
    context_messages: tuple[ToolContextMessageIR, ...] = ()
    # Retained owner diagnostic metadata. Public delivery uses llm_text;
    # neither this field nor structured is automatically appended to it.
    llm_failure_details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not str(self.llm_text or ""):
            raise ValueError("CapabilityResult.llm_text must be non-empty")
        object.__setattr__(self, "affordances", tuple(self.affordances or ()))
        object.__setattr__(self, "recovery_hint", str(self.recovery_hint or ""))
        object.__setattr__(self, "context_messages", tuple(self.context_messages or ()))

    @property
    def user_text(self) -> str:
        """Control replies carry the same owner message and actionable receipt."""
        text = self.llm_text
        if self.status == "ok":
            return text
        if self.effect_receipt is not None:
            text += f"\nEffect: {self.effect_receipt.outcome.value}."
        if self.recovery_hint and self.recovery_hint not in text:
            text += "\n" + self.recovery_hint
        return text


@dataclass(frozen=True)
class ToolCallBudget:
    max_output_chars: int | None = None
    max_output_tokens_estimate: int | None = None
    max_output_bytes: int | None = None
    max_result_spill_chars: int | None = None
    preview_chars: int | None = None
    artifact_bucket_id: str | None = None
    max_read_bytes: int | None = None
    max_lines_to_read: int | None = None
    max_stdout_chars: int | None = None
    timeout_ms: int | None = None


class Plugin(Protocol):
    def register(self, runtime: "ExecutionRuntimePort") -> None:
        ...


class ExecutionProjectionPort(Protocol):
    def project_llm_text(self, value: object) -> str: ...


class ExecutionRuntimePort(ExecutionProjectionPort, Protocol):
    runtime_root: Path | None
    lifecycle_gate: WriterPreferredRWGate
    sync_executor: ThreadPoolExecutor | None
    logical_state: LogicalExecutionStateBackend
    execution_sessions: LogicalExecutionSessions
    result_snapshots: ResultSnapshotStore | None

    @property
    def registry_generation(self) -> ToolRegistryGeneration: ...

    def prepare_prompt_context(self, memory: Any, continuation: Any, *,
        context_view: Any = None, include_live: bool = True) -> None: ...

    def prepare_model_context(self, memory: Any, continuation: Any, *, context_view: Any = None) -> None: ...

    def model_response_received(self, continuation: Any) -> None: ...

    def observe_tool_delivery(self, call: ToolCallIR, result: ToolExecutionResult) -> None: ...

    def stagnation_payload(self, call: ToolCallIR, result: ToolExecutionResult) -> dict[str, Any]: ...

    def bind_result_history(self, memory_service: MemoryServicePort) -> None: ...

    def logical_context_for_turn(self, turn_id: str | None) -> LogicalExecutionContext: ...

    def advance_tool_result_clock(self, *, turn_id: str, clock_id: str,
        retention_steps: int | None = None) -> LogicalExecutionContext: ...

    def retire_tool_results(self, *, turn_id: str | None, result_ids: tuple[str, ...],
        execution_lifetime_id: str = "") -> tuple[str, ...]: ...

    def discard_uncommitted_tool_delivery(self, *, turn_id: str, result_ref: str) -> None: ...

    async def acknowledge_tool_result_async(self, call_id: str, turn_id: str) -> None: ...

    def role_capabilities(self, allowed: list[str]) -> list[str]: ...

    def project_role_descriptor(self, descriptor: CapabilityDescriptor) -> CapabilityDescriptor: ...

    def project_execution_view(self, view: Any) -> Any: ...

    async def complete_evidence(self, call: ToolCallIR, result: ToolExecutionResult,
        **kwargs: Any) -> ToolExecutionResult: ...

    def begin_tool_result_turn(self, *, turn_id: str, scope_key: str = "",
        retention_user_turns: int = ..., input_id: str = "") -> LogicalExecutionContext: ...

    def tool_call_failed_result(self, call: ToolCallIR, exc: Exception) -> ToolExecutionResult:
        ...

    def validate_provider_ref(self, provider_id: str, provider: Any, *, owner_id: str) -> None:
        ...

    def register_provider_ref(self, provider_id: str, provider: Any, *, owner_id: str) -> None:
        ...

    def replace_provider_ref(self, provider_id: str, provider: Any, *, owner_id: str, expected: object) -> None:
        ...

    def unregister_provider_ref(self, provider_id: str, *, expected: object) -> None:
        ...

    def has_registered_capability(self, name: str) -> bool:
        ...

    def resolve_capability_address(self, name: object) -> str:
        ...

    def execute_tool(self, call: Any, *, allow_tools: bool = True, budget: ToolCallBudget | None = None) -> Any:
        ...

    async def execute_tool_async(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: ToolCallBudget | None = None,
        turn_id: str | None = None,
    ) -> ToolExecutionResult:
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
        result_id: str = "",
    ) -> Any:
        ...
