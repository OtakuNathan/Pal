"""Preserve observed effects when role-local authoring fails."""
from dataclasses import dataclass, field
from typing import Any

from pal.bunshin.ipc import BunshinManagerRpcError
from pal.bunshin.submission_errors import SubmissionValidationError, submission_validation
from pal.execution.tool_facade import rejection
from pal.shared import RuntimeStatus, ToolExecutionResult
from pal.shared.diagnostics import exception_report
from pal.shared.tool_protocol import EffectOutcome, FailedResult, RetryDirective, ToolCallIR


@dataclass
class AuthoringProgress:
    started: bool = False
    applied: bool = False
    read_only: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def mutate(self, store: Any, *args: Any, **kwargs: Any) -> Any:
        reducer = kwargs['reducer']

        def validate(payload: dict[str, Any]) -> Any:
            # Reducers operate on a copy, before persistence or the gateway RPC.
            with submission_validation():
                return reducer(payload)

        kwargs['reducer'] = validate
        self.started = True
        result = store.mutate(*args, **kwargs)
        self.applied = True
        self.details['recorded_result'] = dict(result)
        return result


def authoring_error_result(
    call: ToolCallIR, exc: Exception, progress: AuthoringProgress, *, correction: str,
    invalid_code: str = 'invalid_work_item',
) -> ToolExecutionResult:
    cause = exception_report(exc)
    io_cause = isinstance(exc.__cause__, OSError)
    invalid = not progress.applied and not io_cause and (
        isinstance(exc, SubmissionValidationError)
        or isinstance(exc, BunshinManagerRpcError) and exc.kind == 'submission_validation'
        or not progress.started and not progress.read_only and isinstance(exc, ValueError)
    )
    details = {**progress.details, 'error': cause, 'error_type': type(exc).__name__}
    if invalid:
        text = f'{cause} {correction}'
        result = rejection(invalid_code, text, details=details)
    else:
        if progress.applied:
            effect, retry = EffectOutcome.APPLIED, RetryDirective.DO_NOT_RETRY
            advice = ('The reported changes already occurred; a later step failed. Do not repeat completed work. '
                      'Report the failure and recorded state to Manager to recover the unfinished step.')
        elif progress.started:
            effect, retry = EffectOutcome.UNKNOWN, RetryDirective.RECONCILE_FIRST
            advice = ('The operation may have changed state. Reconcile the draft/files with Manager before '
                      'retrying. This is not evidence that the authored content is wrong.')
        else:
            effect = EffectOutcome.NONE if progress.read_only else EffectOutcome.NOT_STARTED
            retry = RetryDirective.SAFE if isinstance(exc, (OSError, BunshinManagerRpcError)) or io_cause else RetryDirective.DO_NOT_RETRY
            advice = ('No mutation was started. Report the runtime or storage failure to Manager; '
                      'do not rewrite content based on this error.')
        text = f'{cause} {advice}'
        if progress.details:
            from pal.shared.result_rendering import render_structured_for_llm
            text += '\nObserved state:\n' + render_structured_for_llm(progress.details)
        result = FailedResult(error_code='authoring_failed', error=cause, llm_text=text,
                              effect=effect, retry=retry, details=details)
    return ToolExecutionResult(name=call.name, call_id=call.call_id, ok=False,
                               text=cause, llm_text=text, structured=details,
                               status=RuntimeStatus.INVALID if invalid else RuntimeStatus.ERROR,
                               invocation_result=result)
