from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, EffectOutcome, FailedResult, RetryDirective
from pal.execution.tool_facade import rejection
from pal.shared.diagnostics import exception_report

from collections.abc import Awaitable, Callable
from typing import Any

from pal.execution.generated_tool_models import (
    BunshinV2AskQuestionInput,
)
from pal.shared import RuntimeStatus, ToolExecutionResult


ASK_QUESTION_CAPABILITY = "op_bunshin_ask_question"

ASK_QUESTION_TOOL_SPEC: dict[str, Any] = {
    "alias": "ask_question",
    "guidance": {
        "search_objects": ('question', 'questions'),
        "purpose": "Suspend the current role invocation and ask the user one decisive question.",
        "use_when": (
            "Use when a contradiction, material ambiguity, infeasible requirement, "
            "missing preference, or scope-changing decision prevents a correct design."
        ),
        "do_not_use_when": (
            "Do not ask about a settled fact, private implementation choice, or decision "
            "that can be derived safely from the bound task and public repository context."
        ),
        "failure_next_steps": (
            "If user interaction is unavailable, do not guess or silently reinterpret the "
            "task; report the blocked requirement through the harness."
        ),
    },
    "InputModel": BunshinV2AskQuestionInput,
}


async def ask_question_tool_result(
    call: ToolCallIR,
    *,
    request_user: (
        Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None
    ),
) -> ToolExecutionResult:
    if request_user is None:
        return _question_failure(
            call, "user_interaction_unavailable",
            "Architect user interaction is unavailable. Report the environment blocker to Manager; "
            "do not guess a material requirement or preference.",
            effect=EffectOutcome.NOT_STARTED, retry=RetryDirective.DO_NOT_RETRY,
        )
    request_started = False
    answer = ""
    revision: dict[str, Any] = {}
    try:
        args = dict(call.args or {})
        title = str(args.get("title") or "").strip()
        question = str(args.get("question") or "").strip()
        if not title or not question:
            raise ValueError("ask_question requires title and question")
        options: list[dict[str, str]] = []
        for index in range(1, 4):
            option = str(args.get(f"option_{index}") or "").strip()
            if not option:
                raise ValueError(
                    f"ask_question requires option_{index}"
                )
            options.append(
                {"label": option, "description": option}
            )
        request_started = True
        response = await request_user(
            {
                "title": title,
                "questions": [
                    {
                        "id": "architecture-question",
                        "title": title,
                        "question": question,
                        "options": options,
                    }
                ],
            }
        )
        answers = [
            dict(item or {})
            for item in list(response.get("answers") or [])
        ]
        answer = (
            str(answers[0].get("answer") or "") if answers else ""
        )
        if not answer.strip():
            return _question_failure(
                call, "user_question_cancelled",
                "The user did not answer. Keep the ambiguity explicit; do not submit a contract "
                "that guesses the answer or automatically repeat the question. Report the blocker to Manager.",
                effect=EffectOutcome.APPLIED, retry=RetryDirective.DO_NOT_RETRY,
                details={"status": "cancelled"},
            )
        revision = dict(response.get("task_revision") or {})
        if not bool(revision.get("appended")):
            raise RuntimeError(
                "Manager returned an answer without appending task.yaml"
            )
        return ToolExecutionResult(
            name=call.name,
            ok=True,
            text=f"User answered: {answer}",
            llm_text=(
                f"User answered: {answer}\n"
                "Manager already appended this exact exchange as the newest "
                "task.yaml revision. Continue directly; do not edit or "
                "restate the task ledger."
            ),
            structured={
                "status": "answered_revision_recorded",
                "answer": answer,
                "task_revision": revision,
            },
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        cause = exception_report(exc)
        details = {"error": cause, "error_type": type(exc).__name__}
        if answer.strip():
            details.update(answer=answer, task_revision=revision, status="answered_revision_unconfirmed")
            return _question_failure(
                call, "question_revision_unconfirmed",
                f"User answered: {answer}\n{cause}\n"
                "The answer was received, but recording it in task.yaml is unconfirmed. "
                "Do not ask the user again or edit the task ledger. Report the answer and "
                "recording failure to Manager for reconciliation before submitting the contract.",
                effect=EffectOutcome.APPLIED, retry=RetryDirective.DO_NOT_RETRY, details=details,
            )
        if request_started:
            return _question_failure(
                call, "user_question_outcome_unknown",
                cause + " The question may already have been delivered or answered. "
                "Ask Manager to reconcile the interaction and task revision before retrying; "
                "do not automatically repeat the question.",
                effect=EffectOutcome.UNKNOWN, retry=RetryDirective.RECONCILE_FIRST, details=details,
            )
        return _question_failure(
            call, "invalid_question", cause + " Correct the question arguments before retrying.",
            effect=EffectOutcome.NOT_STARTED, retry=RetryDirective.CORRECT_INPUT, details=details,
        )


def _question_failure(
    call: ToolCallIR, code: str, text: str, *, effect: EffectOutcome,
    retry: RetryDirective, details: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    details = dict(details or {})
    result = (
        rejection(code, text, retry=retry, details=details)
        if effect is EffectOutcome.NOT_STARTED else
        FailedResult(error_code=code, error=text, llm_text=text,
                     effect=effect, retry=retry, details=details)
    )
    return ToolExecutionResult(
        name=call.name, call_id=call.call_id, ok=False, text=text, llm_text=text,
        structured=details, invocation_result=result,
        status=RuntimeStatus.INVALID if code == "invalid_question" else RuntimeStatus.ERROR,
    )
