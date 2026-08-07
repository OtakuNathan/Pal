from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, model_validator

from pal.shared.tool_protocol import (
    CompleteResult,
    EffectOutcome,
    FailedResult,
    PagedResult,
    RejectedResult,
    RetryDirective,
    TOOL_INVOCATION_RESULT_ADAPTER,
    ToolAffordance,
    ToolInvocationResult,
)


class StrictToolModel(BaseModel):
    """Base contract for Pal-owned tool inputs and outputs."""

    model_config = ConfigDict(strict=True, extra="forbid")


class EmptyToolInput(StrictToolModel):
    pass


class EmptyToolOutput(StrictToolModel):
    pass


class StructuredToolOutput(RootModel[dict[str, Any]]):
    """Strict top-level object for capabilities without a narrower output model.

    This model validates the handler's real top-level object and does not
    invent a compatibility wrapper around provider-defined values.
    """

    model_config = ConfigDict(strict=True)


class McpToolOutput(StrictToolModel):
    """Stable output shape used when an MCP server declares no output schema."""

    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


@lru_cache(maxsize=None)
def model_validation_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Compile each immutable Pydantic tool contract once per process."""

    return model.model_json_schema(mode="validation")


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


class ToolGuidance(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    purpose: str
    use_when: str = ""
    do_not_use_when: str = ""
    failure_next_steps: str = ""


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
    fallback_description: str | None = None,
) -> str:
    enum_values = _schema_enum_values(input_schema)
    # Display contract: guidance.purpose is the display source of truth;
    # the raw description is only the fallback when purpose is absent/empty.
    purpose = str(guidance.purpose or fallback_description or "").strip()
    sections = [
        f"Purpose: {purpose}",
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
    "PagedResult",
    "PagingMode",
    "RejectedResult",
    "RetryDirective",
    "RetryPolicy",
    "StrictToolModel",
    "StructuredToolOutput",
    "ToolAffordance",
    "ToolExecutionSemantics",
    "ToolGuidance",
    "ToolHandlerResult",
    "ToolExecutionError",
    "ToolRejectedError",
    "ToolInvocationResult",
    "compile_tool_description",
    "derive_retry_directive",
    "model_validation_schema",
    "rejection",
    "validate_output",
]
