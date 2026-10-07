"""Bounded, content-free verifier telemetry; never a tool-output log."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import sqlite3
import sys
from types import FunctionType
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pal.bunshin.ipc import BunshinManagerRpcError
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


# Finite source tokens, resolved to exact loaded code objects before capture.
# A fabricated co_filename/co_name or a dynamically named exception is not a
# source identity and is never copied into this operational event.
_FRAME_SOURCES = {
    "pal.bunshin.semantic_evidence": (
        "run_shell_evidence", "run_lsp_evidence", "_required_text", "_runtime_root",
        "_artifact_store", "execution_workspace_fingerprint", "_integer_list",
    ),
    "pal.bunshin.verification_builder": (
        "verification_builder_tool_result", "_assert_tool_contract_allows",
        "_preflight_verification_case_execution", "_require_adapter", "_store_context",
    ),
    "pal.bunshin.swe_verification": ("swe_verification_tool_result",),
    "pal.bunshin.submission_drafts": (
        "SubmissionDraftContext.from_workspace", "SubmissionDraftStore.read",
        "SubmissionDraftStore._assert_authoring_contract", "SubmissionDraftStore._assert_fence",
        "SubmissionDraftStore._read_or_create_locked", "SubmissionDraftStore._inherited_payload_locked",
        "SubmissionDraftStore._ensure_schema", "decode_remote_draft_snapshot",
    ),
    "pal.bunshin.role_contracts": ("RoleActivation.from_values", "RoleActivation.__post_init__"),
    "pal.bunshin.ipc": ("BunshinRoleGatewayClient.request_sync", "BunshinRoleGatewayClient.request"),
}
_FRAME_TOKENS = frozenset((module.rsplit(".", 1)[-1] + ".py", function)
                         for module, functions in _FRAME_SOURCES.items() for function in functions)
_ERROR_TYPES: dict[type[BaseException], str] = {error: error.__name__ for error in (
    ValueError, TypeError, RuntimeError, KeyError, OSError, FileNotFoundError,
    PermissionError, TimeoutError, sqlite3.OperationalError, BunshinManagerRpcError, ValidationError,
)}


class VerifierFailureFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    file: str = Field(max_length=64)
    function: str = Field(max_length=64)
    line: int = Field(gt=0, lt=1_000_000_000)

    @model_validator(mode="after")
    def known_source(self) -> "VerifierFailureFrame":
        if (self.file, self.function) not in _FRAME_TOKENS:
            raise ValueError("unknown verifier source frame")
        return self


class VerifierFailureProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    error_type: str = Field(max_length=32)
    frames: list[VerifierFailureFrame] = Field(max_length=6)

    @field_validator("error_type")
    @classmethod
    def known_type(cls, value: str) -> str:
        if value not in {*_ERROR_TYPES.values(), "other"}:
            raise ValueError("unknown verifier exception type")
        return value


@dataclass
class VerifierFailureCapture:
    provenance: VerifierFailureProvenance | None = None


_FAILURE_CAPTURE: ContextVar[VerifierFailureCapture | None] = ContextVar("verifier_failure_capture", default=None)


@contextmanager
def capture_verifier_failure(*, enabled: bool) -> Iterator[VerifierFailureCapture]:
    # Heartbeat creates a child task. Share this per-call holder through the
    # inherited context, never set a new child ContextVar value or global sink.
    capture = VerifierFailureCapture()
    token = _FAILURE_CAPTURE.set(capture if enabled else None)
    try:
        yield capture
    finally:
        _FAILURE_CAPTURE.reset(token)


def record_verifier_failure(exc: Exception) -> None:
    """Best-effort pre-conversion capture; no change to the tool's result."""
    capture = _FAILURE_CAPTURE.get()
    if capture is None or capture.provenance is not None:
        return
    try:
        codes = {}
        for module_name, functions in _FRAME_SOURCES.items():
            module = sys.modules.get(module_name)
            if module is None:
                continue
            for function in functions:
                value: object = module
                for part in function.split("."):
                    value = vars(value).get(part)
                    if value is None:
                        break
                if isinstance(value, (classmethod, staticmethod)):
                    value = value.__func__
                if isinstance(value, FunctionType):
                    codes[id(value.__code__)] = (module_name.rsplit(".", 1)[-1] + ".py", function)
        frames: deque[VerifierFailureFrame] = deque(maxlen=6)
        traceback = exc.__traceback__
        while traceback is not None:
            source = codes.get(id(traceback.tb_frame.f_code))
            if source is not None:
                frames.append(VerifierFailureFrame(file=source[0], function=source[1], line=traceback.tb_lineno))
            traceback = traceback.tb_next
        capture.provenance = VerifierFailureProvenance(
            error_type=_ERROR_TYPES.get(type(exc), "other"), frames=list(frames),
        )
    except Exception:
        # Telemetry failure must not replace the original failure or result.
        return


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
    provenance: VerifierFailureProvenance | None = None

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
    provenance: VerifierFailureProvenance | None = None,
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
            provenance=provenance,
        ).model_dump()
    except ValidationError:
        # A malformed diagnostic must not change tool execution semantics.
        return None


def _allowed_value(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and len(value) <= 64 and value in allowed else fallback
