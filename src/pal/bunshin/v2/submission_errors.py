from __future__ import annotations

from contextlib import contextmanager

from pal.bunshin.ipc import BunshinManagerRpcError
from pal.execution.tool_facade import rejection
from pal.shared import RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import EffectOutcome, FailedResult, RetryDirective, ToolCallIR


class SubmissionValidationError(ValueError):
    """An authored submission failed a Manager content check before acceptance."""


@contextmanager
def submission_validation():
    try:
        yield
    except ValueError as exc:
        if isinstance(exc.__cause__, OSError):
            raise
        raise SubmissionValidationError(str(exc)) from exc


def role_gateway_error_kind(exc: Exception) -> str:
    return "submission_validation" if isinstance(exc, SubmissionValidationError) else "role_gateway"


def submission_error_result(
    call: ToolCallIR, exc: Exception, *, submission_started: bool,
    invalid_code: str, correction: str,
) -> ToolExecutionResult:
    remote_validation = isinstance(exc, BunshinManagerRpcError) and exc.kind == "submission_validation"
    # YAML parsing can wrap filesystem errors. Preserve their infrastructure meaning.
    filesystem_cause = isinstance(exc.__cause__, OSError)
    invalid = remote_validation or (
        isinstance(exc, SubmissionValidationError) and not filesystem_cause
    )
    text = f"{type(exc).__name__}: {exc}"
    if invalid:
        category = "validation"
        advice = correction
    elif submission_started:
        category = "submission_outcome_unknown"
        advice = (
            "Submission acceptance could not be confirmed. The runtime must reconcile "
            "the Manager receipt before resubmitting. Preserve the current artifacts, "
            "checklist, and findings; this is not evidence of a content defect."
        )
    else:
        category = "submission_infrastructure_error"
        advice = (
            "Submission preparation failed in the runtime or Manager gateway. "
            "Preserve the current artifacts, checklist, and findings. Recover the "
            "service or storage failure before retrying; do not repair content based on this error."
        )
    details = {"error": str(exc), "error_type": type(exc).__name__, "error_category": category}
    if isinstance(exc, BunshinManagerRpcError):
        details["gateway_error_kind"] = exc.kind
    llm_text = f"{text} {advice}"
    if invalid:
        result = rejection(invalid_code, llm_text, details=details)
    else:
        result = FailedResult(
            error_code=category, error=llm_text, llm_text=llm_text, details=details,
            effect=EffectOutcome.UNKNOWN if submission_started else EffectOutcome.NOT_STARTED,
            retry=(RetryDirective.RECONCILE_FIRST if submission_started else
                   RetryDirective.SAFE if isinstance(exc, (OSError, BunshinManagerRpcError)) or filesystem_cause
                   else RetryDirective.DO_NOT_RETRY),
        )
    return ToolExecutionResult(
        name=call.name, call_id=call.call_id, ok=False, text=text, llm_text=llm_text,
        structured=details, status=RuntimeStatus.INVALID if invalid else RuntimeStatus.ERROR,
        invocation_result=result,
    )
