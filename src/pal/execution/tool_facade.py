from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter, ValidationError, model_validator


class StrictToolModel(BaseModel):
    """Base contract for Pal-owned tool inputs and outputs."""

    model_config = ConfigDict(strict=True, extra="forbid")


class EmptyToolInput(StrictToolModel):
    pass


class EmptyToolOutput(StrictToolModel):
    pass


class OpaqueToolOutput(RootModel[dict[str, Any]]):
    """Explicit dictionary output for capabilities without a narrower shape."""

    model_config = ConfigDict(strict=True)


class McpToolOutput(StrictToolModel):
    """Stable output shape used when an MCP server declares no output schema."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


class InvocationMode(str, Enum):
    DIRECT = "direct"
    INDIRECT = "indirect"


class EffectKind(str, Enum):
    NONE = "none"
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    CONTROL = "control"


class Idempotency(str, Enum):
    IDEMPOTENT = "idempotent"
    KEYED_IDEMPOTENT = "keyed_idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class RetryPolicy(str, Enum):
    AUTOMATIC = "automatic"
    RECONCILE_FIRST = "reconcile_first"
    NEVER_AUTOMATIC = "never_automatic"


class PagingMode(str, Enum):
    NEVER = "never"
    SUPPORTED = "supported"


class EffectOutcome(str, Enum):
    NONE = "none"
    NOT_STARTED = "not_started"
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class RetryDirective(str, Enum):
    CORRECT_INPUT = "correct_input"
    SAFE = "safe"
    RECONCILE_FIRST = "reconcile_first"
    DO_NOT_RETRY = "do_not_retry"


class ToolGuidance(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    purpose: str
    use_when: str
    do_not_use_when: str
    failure_next_steps: str


class ToolExecutionSemantics(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    invocation_mode: InvocationMode
    effect_kind: EffectKind
    idempotency: Idempotency
    retry_policy: RetryPolicy
    paging: PagingMode

    @model_validator(mode="after")
    def validate_retry_safety(self) -> "ToolExecutionSemantics":
        if self.idempotency is Idempotency.NON_IDEMPOTENT and self.retry_policy is RetryPolicy.AUTOMATIC:
            raise ValueError("non_idempotent tools cannot use automatic retry")
        if self.effect_kind is EffectKind.NONE and self.idempotency is not Idempotency.IDEMPOTENT:
            raise ValueError("effect_kind=none must be idempotent")
        return self


class EffectReceipt(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    outcome: EffectOutcome
    receipt: dict[str, Any] = Field(default_factory=dict)


class ToolAffordance(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


class ToolHandlerResult(StrictToolModel):
    """Successful handler return with an optional, explicit effect receipt."""

    output: Any
    llm_text: str = ""
    effect_receipt: EffectReceipt | None = None
    affordances: list[ToolAffordance] = Field(default_factory=list)


class ToolExecutionError(RuntimeError):
    """Handler failure that may carry a trustworthy effect receipt."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "handler_failed",
        effect_receipt: EffectReceipt | None = None,
        affordances: list[ToolAffordance] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "handler_failed")
        self.effect_receipt = effect_receipt
        self.affordances = list(affordances or ())
        self.details = dict(details or {})


class ToolRejectedError(ValueError):
    """Pre-effect refusal with a correctable input or state precondition."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "rejected",
        retry: RetryDirective = RetryDirective.CORRECT_INPUT,
        affordances: list[ToolAffordance] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "rejected")
        self.retry = retry
        self.affordances = list(affordances or ())
        self.details = dict(details or {})


T = TypeVar("T")


class CompleteResult(StrictToolModel, Generic[T]):
    kind: Literal["complete"] = "complete"
    output: T
    effect: EffectOutcome
    llm_text: str
    affordances: list[ToolAffordance] = Field(default_factory=list)
    context_delivery: dict[str, Any] | None = Field(default=None, exclude=True)


class PagedResult(StrictToolModel):
    kind: Literal["paged"] = "paged"
    result_handle: dict[str, Any]
    page_text: str
    effect: EffectOutcome
    llm_text: str
    affordances: list[ToolAffordance]
    context_delivery: dict[str, Any] | None = Field(default=None, exclude=True)


class RejectedResult(StrictToolModel):
    kind: Literal["rejected"] = "rejected"
    error_code: str
    error: str
    effect: Literal[EffectOutcome.NOT_STARTED] = EffectOutcome.NOT_STARTED
    retry: RetryDirective
    llm_text: str
    affordances: list[ToolAffordance] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class FailedResult(StrictToolModel):
    kind: Literal["failed"] = "failed"
    error_code: str
    error: str
    effect: EffectOutcome
    retry: RetryDirective
    llm_text: str
    affordances: list[ToolAffordance] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


ToolInvocationResult = Annotated[
    CompleteResult[Any] | PagedResult | RejectedResult | FailedResult,
    Field(discriminator="kind"),
]
TOOL_INVOCATION_RESULT_ADAPTER = TypeAdapter(ToolInvocationResult)


@dataclass(frozen=True)
class Tool:
    """One immutable Pal-owned tool specification.

    Registration consumes this value as a whole.  Contracts, prose, execution
    semantics, examples, and the callable therefore cannot drift independently.
    """

    alias: str
    canonical_path: str
    InputModel: type[BaseModel]
    OutputModel: type[BaseModel]
    guidance: ToolGuidance
    execution: ToolExecutionSemantics
    search_text: str
    handler: Any
    examples: tuple[dict[str, Any], ...] = ()
    module_id: str = ""
    family: str = "general"
    source: str = "internal"
    target_id: str = "__singleton__"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        alias = str(self.alias or "").strip()
        canonical_path = str(self.canonical_path or "").strip()
        if not alias:
            raise ValueError("tool alias is required")
        if not canonical_path:
            raise ValueError("tool canonical_path is required")
        if alias == canonical_path:
            raise ValueError("tool alias must not expose canonical_path")
        if len(alias) > 64 or re.fullmatch(r"[A-Za-z0-9_-]+", alias) is None:
            raise ValueError("invalid tool alias; expected 1-64 characters from [A-Za-z0-9_-]")
        if alias.startswith(("op_", "intro_")):
            raise ValueError("tool alias must not use the reserved canonical-path namespace")
        _validate_model_class("InputModel", self.InputModel)
        _validate_model_class("OutputModel", self.OutputModel)
        _validate_model_defaults(self.InputModel)
        _validate_model_defaults(self.OutputModel)
        schema = self.InputModel.model_json_schema(mode="validation")
        if schema.get("properties") and not self.examples:
            raise ValueError(f"non-empty InputModel for {alias!r} requires at least one example")
        for example in self.examples:
            self.InputModel.model_validate(example, strict=True)
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "canonical_path", canonical_path)
        object.__setattr__(
            self,
            "examples",
            tuple(MappingProxyType(_freeze_plain_dict(item)) for item in self.examples),
        )
        object.__setattr__(self, "metadata", MappingProxyType(_freeze_plain_dict(self.metadata)))


def validate_output(model: type[BaseModel], value: Any) -> BaseModel:
    """Use one adapter for Pydantic instances, dictionaries, and JSON strings."""

    if isinstance(value, str):
        return model.model_validate_json(value, strict=True)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    return model.model_validate(value, strict=True)


def derive_retry_directive(
    semantics: ToolExecutionSemantics,
    outcome: EffectOutcome,
    *,
    correctable_input: bool = False,
) -> RetryDirective:
    if correctable_input:
        return RetryDirective.CORRECT_INPUT
    if semantics.retry_policy is RetryPolicy.NEVER_AUTOMATIC:
        return RetryDirective.DO_NOT_RETRY
    if outcome in {EffectOutcome.NONE, EffectOutcome.NOT_STARTED, EffectOutcome.NOT_APPLIED}:
        return RetryDirective.SAFE
    if outcome is EffectOutcome.UNKNOWN:
        if semantics.idempotency is Idempotency.NON_IDEMPOTENT:
            return RetryDirective.RECONCILE_FIRST
        return (
            RetryDirective.RECONCILE_FIRST
            if semantics.retry_policy is RetryPolicy.RECONCILE_FIRST
            else RetryDirective.SAFE
        )
    if semantics.idempotency is Idempotency.NON_IDEMPOTENT and outcome is EffectOutcome.APPLIED:
        return RetryDirective.DO_NOT_RETRY
    if semantics.retry_policy is RetryPolicy.RECONCILE_FIRST:
        return RetryDirective.RECONCILE_FIRST
    return RetryDirective.SAFE


def compile_tool_description(
    *,
    alias: str,
    guidance: ToolGuidance,
    execution: ToolExecutionSemantics,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    example: dict[str, Any] | None,
) -> str:
    enum_values = _schema_enum_values(input_schema)
    sections = [
        f"Purpose: {guidance.purpose}",
        f"Use when: {guidance.use_when}",
        f"Do not use when: {guidance.do_not_use_when}",
        f"Failure next steps: {guidance.failure_next_steps}",
        (
            "Execution semantics: "
            f"invocation_mode={execution.invocation_mode.value}; "
            f"effect_kind={execution.effect_kind.value}; "
            f"idempotency={execution.idempotency.value}; "
            f"retry_policy={execution.retry_policy.value}; "
            f"paging={execution.paging.value}."
        ),
    ]
    if enum_values:
        rendered = "; ".join(f"{path}={json.dumps(values, ensure_ascii=False)}" for path, values in enum_values)
        sections.append(f"Valid enum/const values: {rendered}")
    if example is not None:
        sections.append(f"Valid example: {json.dumps(example, ensure_ascii=False, sort_keys=True)}")
    sections.extend(
        [
            f"Output shape: {json.dumps(output_schema, ensure_ascii=False, sort_keys=True)}",
            f"Invoke using the exact alias `{alias}`.",
        ]
    )
    return "\n".join(sections)


def rejection(
    error_code: str,
    error: str,
    *,
    retry: RetryDirective = RetryDirective.CORRECT_INPUT,
    affordances: list[ToolAffordance] | None = None,
    details: dict[str, Any] | None = None,
) -> RejectedResult:
    return RejectedResult(
        error_code=error_code,
        error=error,
        retry=retry,
        llm_text=error,
        affordances=list(affordances or ()),
        details=dict(details or {}),
    )


def validation_error_details(exc: ValidationError) -> dict[str, Any]:
    return {"validation_errors": exc.errors(include_url=False, include_input=False)}


def _validate_model_class(label: str, model: type[BaseModel]) -> None:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError(f"{label} must be a Pydantic v2 BaseModel subclass")


def _validate_model_defaults(model: type[BaseModel]) -> None:
    config = model.model_config
    if config.get("strict") is not True:
        raise ValueError(f"{model.__name__} must set strict=True")
    if not issubclass(model, RootModel) and config.get("extra") != "forbid":
        raise ValueError(f"{model.__name__} must set extra='forbid'")


def _schema_enum_values(schema: Any, path: str = "$") -> list[tuple[str, list[Any]]]:
    values: list[tuple[str, list[Any]]] = []
    if isinstance(schema, dict):
        if isinstance(schema.get("enum"), list):
            values.append((path, list(schema["enum"])))
        if "const" in schema:
            values.append((path, [schema["const"]]))
        for key, item in schema.items():
            if key in {"enum", "const"}:
                continue
            values.extend(_schema_enum_values(item, f"{path}.{key}"))
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            values.extend(_schema_enum_values(item, f"{path}[{index}]"))
    return values


def _freeze_plain_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: _freeze_plain_value(item) for key, item in dict(value or {}).items()}


def _freeze_plain_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(_freeze_plain_dict(value))
    if isinstance(value, list | tuple):
        return tuple(_freeze_plain_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_plain_value(item) for item in value)
    return value


__all__ = [
    "CompleteResult",
    "EffectKind",
    "EffectOutcome",
    "EffectReceipt",
    "EmptyToolInput",
    "EmptyToolOutput",
    "FailedResult",
    "Idempotency",
    "InvocationMode",
    "McpToolOutput",
    "OpaqueToolOutput",
    "PagedResult",
    "PagingMode",
    "RejectedResult",
    "RetryDirective",
    "RetryPolicy",
    "StrictToolModel",
    "Tool",
    "ToolAffordance",
    "ToolExecutionSemantics",
    "ToolGuidance",
    "ToolHandlerResult",
    "ToolExecutionError",
    "ToolRejectedError",
    "ToolInvocationResult",
    "compile_tool_description",
    "derive_retry_directive",
    "rejection",
    "validate_output",
]
