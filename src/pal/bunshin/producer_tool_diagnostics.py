"""Bounded, content-free implementation telemetry; never a tool-output log.

Retain only the first 128 records per persistent logical invocation, across
attempts and restarts. Later records are dropped without stopping any tools.
Consequently, absence after the cap is not evidence that a call did not occur.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pal.shared import BunshinInvocationPack, ToolExecutionResult
from pal.shared.tool_protocol import FailedResult, RejectedResult, ToolCallIR


# A fixed budget per invocation, including discovery and both call stages.
# Worker emission and Manager storage attempts are separately bounded; storage
# enforces the same durable limit across worker/Manager reconstruction.
MAX_PRODUCER_TOOL_DIAGNOSTICS = 128

# Do not derive these tokens from a registry, arguments, or exception strings.
_PRODUCER_ALIASES = {
    "op_bunshin_candidate_submit": "submit_candidate",
    "op_bunshin_update_checklist": "update_checklist",
    "op_bunshin_candidate_report_architecture_defect": "report_candidate_architecture_defect",
    "op_bunshin_candidate_request_module_split": "request_candidate_module_split",
}
_ALIASES = frozenset(_PRODUCER_ALIASES.values())
_ERROR_CODES = frozenset({
    "unknown_tool", "wrong_invocation_mode", "invalid_arguments", "invalid",
    "capability_not_allowed", "capability_denied_by_bunshin_policy",
    "approval_not_accepted", "handler_failed", "handler_exception",
    "missing_effect_receipt", "output_validation_failed", "rejected",
    "submission_infrastructure_error", "submission_outcome_unknown",
    "checklist_invalid", "candidate_workspace_polluted", "candidate_product_required",
    "validation", "tool_execution_exception", "unclassified_error",
})
_STATUSES = frozenset({
    "ok", "error", "queued", "unsupported", "invalid", "not_found",
    "retry", "skipped", "forbidden", "unknown", *_ERROR_CODES,
})


class ProducerToolDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    phase: Literal["producer_tool_call"] = "producer_tool_call"
    round: int = Field(ge=0, le=1_000_000_000)
    tool_call_index: int = Field(ge=0, le=1_000_000_000)
    tool_alias: str = Field(max_length=64)
    route: Literal["direct", "call_tool", "read_tool"] = "direct"
    stage: Literal["started", "completed", "failed"]
    ok: bool | None = None
    status: str = Field(default="unknown", max_length=64)
    error_code: str = Field(default="", max_length=64)
    # Manager replaces this from the process owner's envelope.
    attempt_id: str = Field(default="", max_length=28, pattern=r"^(?:att_[0-9a-f]{24})?$")

    @field_validator("tool_alias")
    @classmethod
    def known_alias(cls, value: str) -> str:
        if value not in _ALIASES:
            raise ValueError("unknown producer alias")
        return value

    @field_validator("status")
    @classmethod
    def known_status(cls, value: str) -> str:
        if value not in _STATUSES:
            raise ValueError("unknown producer status")
        return value

    @field_validator("error_code")
    @classmethod
    def known_error_code(cls, value: str) -> str:
        if value and value not in _ERROR_CODES:
            raise ValueError("unknown producer error code")
        return value


def is_producer_pack(pack: BunshinInvocationPack) -> bool:
    # Profiles are presentation, not authority; other roles use checklists too.
    binding = (pack.metadata or {}).get("bunshin_v2")
    return isinstance(binding, dict) and binding.get("role") == "implementation"


def producer_tool_alias(call: ToolCallIR) -> str:
    name: object = call.name
    if name in {"call_tool", "op_tool_call", "read_tool", "op_tool_read"}:
        name = call.arguments.get("name")
    if not isinstance(name, str) or len(name) > 80:
        return ""
    return _PRODUCER_ALIASES.get(name, name if name in _ALIASES else "")


def producer_tool_diagnostic(
    call: ToolCallIR, *, round_index: int, tool_call_index: int,
    stage: Literal["started", "completed", "failed"],
    result: ToolExecutionResult | None = None,
) -> dict[str, Any] | None:
    alias = producer_tool_alias(call)
    if not alias:
        return None
    status, error_code, ok = "unknown", "", None
    if result is not None:
        ok = result.ok if type(result.ok) is bool else None
        status = _allowed_value(result.status, _STATUSES, "unknown")
        invocation = result.invocation_result
        structured = result.structured if isinstance(result.structured, dict) else {}
        code = (
            invocation.error_code if isinstance(invocation, (FailedResult, RejectedResult))
            else structured.get("error_code") or structured.get("reason")
        )
        if ok is False:
            error_code = _allowed_value(code, _ERROR_CODES, "unclassified_error")
    if stage == "failed":
        ok, status, error_code = False, "error", "tool_execution_exception"
    try:
        return ProducerToolDiagnostic(
            round=round_index, tool_call_index=tool_call_index, tool_alias=alias,
            route=("call_tool" if call.name in {"call_tool", "op_tool_call"}
                   else "read_tool" if call.name in {"read_tool", "op_tool_read"} else "direct"),
            stage=stage, ok=ok, status=status, error_code=error_code,
        ).model_dump()
    except ValidationError:
        return None


def _allowed_value(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and len(value) <= 64 and value in allowed else fallback
