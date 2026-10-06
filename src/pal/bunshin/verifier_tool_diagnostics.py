"""Bounded, content-free verifier telemetry; never a tool-output log."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pal.shared import BunshinInvocationPack, ToolExecutionResult
from pal.shared.tool_protocol import FailedResult, RejectedResult, ToolCallIR


# Deliberately explicit: new tools and error strings must not silently become
# durable telemetry. Never derive this allowlist from a runtime tool registry.
_VERIFIER_ALIASES = {
    "op_bunshin_verification_scratch_write": "write_verification_scratch",
    "op_bunshin_verification_run_historical_regression": "run_verification_historical_regression",
    "op_bunshin_verification_run_diff_risk": "run_verification_diff_risk",
    "op_bunshin_verification_run_adversarial_case": "run_verification_adversarial_case",
    "op_bunshin_verification_run_focused_test": "run_verification_focused_test",
    "op_bunshin_verification_run_compile_check": "run_verification_compile_check",
    "op_bunshin_verification_run_warning_check": "run_verification_warning_check",
    "op_bunshin_verification_run_consumer_probe": "run_verification_consumer_probe",
    "op_bunshin_verification_run_dogfood": "run_verification_dogfood",
    "op_bunshin_verification_run_platform_probe": "run_verification_platform_probe",
    "op_bunshin_verification_run_lsp_check": "run_verification_lsp_check",
    "op_bunshin_verification_check_unavailable": "record_unavailable_verification",
    "op_bunshin_verification_set_summary": "set_verification_summary",
    "op_bunshin_verification_draft_status": "read_verification_draft_status",
    "op_bunshin_verification_remove_case": "remove_verification_case",
    "op_bunshin_verification_submit": "submit_verification",
    "op_bunshin_verification_pass": "submit_verification_pass",
    "op_bunshin_verification_request_module_repair": "request_verification_module_repair",
    "op_bunshin_verification_request_contract_revision": "request_verification_contract_revision",
    "op_bunshin_verification_request_architecture_revision": "request_verification_architecture_revision",
    "op_bunshin_verification_request_requirements_revision": "request_verification_requirements_revision",
    "op_bunshin_verification_unknown": "submit_verification_unknown",
}
_ALIASES = frozenset(_VERIFIER_ALIASES.values())
_ERROR_CODES = frozenset({
    "unknown_tool", "wrong_invocation_mode", "invalid_arguments", "invalid",
    "capability_not_allowed", "capability_denied_by_bunshin_policy",
    "approval_not_accepted", "handler_failed", "handler_exception",
    "missing_effect_receipt", "output_validation_failed", "rejected",
    "submission_infrastructure_error", "submission_outcome_unknown",
    "validation", "tool_execution_exception", "unclassified_error",
})
_STATUSES = frozenset({
    "ok", "error", "queued", "unsupported", "invalid", "not_found",
    "retry", "skipped", "forbidden", "unknown", *_ERROR_CODES,
})


class VerifierToolDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    phase: Literal["verifier_tool_call"] = "verifier_tool_call"
    round: int = Field(ge=0, le=1_000_000_000)
    tool_call_index: int = Field(ge=0, le=1_000_000_000)
    tool_alias: str = Field(max_length=64)
    route: Literal["direct", "call_tool", "read_tool"] = "direct"
    stage: Literal["started", "completed", "failed"]
    ok: bool | None = None
    status: str = Field(default="unknown", max_length=64)
    error_code: str = Field(default="", max_length=64)
    # Filled by Manager from the process owner's envelope, never worker data.
    attempt_id: str = Field(default="", pattern=r"^(?:att_[0-9a-f]{24})?$")

    @field_validator("tool_alias")
    @classmethod
    def known_alias(cls, value: str) -> str:
        if value not in _ALIASES:
            raise ValueError("unknown verifier alias")
        return value

    @field_validator("status")
    @classmethod
    def known_status(cls, value: str) -> str:
        if value not in _STATUSES:
            raise ValueError("unknown verifier status")
        return value

    @field_validator("error_code")
    @classmethod
    def known_error_code(cls, value: str) -> str:
        if value and value not in _ERROR_CODES:
            raise ValueError("unknown verifier error code")
        return value


def is_verifier_pack(pack: BunshinInvocationPack) -> bool:
    binding = dict((pack.metadata or {}).get("bunshin_v2") or {})
    return binding.get("role") == "verifier" or pack.bunshin_profile == "bunshin_v2.verifier"


def verifier_tool_alias(call: ToolCallIR) -> str:
    name: object = call.name
    # Inspect only the fixed routing key, including failed pre-handler routes.
    if name in {"call_tool", "op_tool_call", "read_tool", "op_tool_read"}:
        name = call.arguments.get("name")
    if not isinstance(name, str) or len(name) > 80:
        return ""
    return _VERIFIER_ALIASES.get(name, name if name in _ALIASES else "")


def verifier_tool_diagnostic(
    call: ToolCallIR, *, round_index: int, tool_call_index: int,
    stage: Literal["started", "completed", "failed"],
    result: ToolExecutionResult | None = None,
) -> dict[str, Any] | None:
    alias = verifier_tool_alias(call)
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
        return VerifierToolDiagnostic(
            round=round_index, tool_call_index=tool_call_index, tool_alias=alias,
            route=("call_tool" if call.name in {"call_tool", "op_tool_call"}
                   else "read_tool" if call.name in {"read_tool", "op_tool_read"} else "direct"),
            stage=stage, ok=ok, status=status, error_code=error_code,
        ).model_dump()
    except ValidationError:
        # A malformed diagnostic must not change tool execution semantics.
        return None


def _allowed_value(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and len(value) <= 64 and value in allowed else fallback
