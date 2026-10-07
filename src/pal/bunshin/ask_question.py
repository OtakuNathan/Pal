from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

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
        return ToolExecutionResult(
            name=call.name,
            ok=False,
            text="Architect user interaction is unavailable in this runtime",
            llm_text=(
                "Architect user interaction is unavailable. Do not guess a "
                "material requirement or preference."
            ),
            structured={"reason": "user_interaction_unavailable"},
            call_id=call.call_id,
            status=RuntimeStatus.ERROR,
        )
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
            return ToolExecutionResult(
                name=call.name,
                ok=False,
                text="Architect user question was cancelled",
                llm_text=(
                    "The user did not answer. Keep the ambiguity explicit; "
                    "do not submit a contract that guesses the answer."
                ),
                structured={"status": "cancelled"},
                call_id=call.call_id,
                status=RuntimeStatus.ERROR,
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
        text = f"{exc.__class__.__name__}: {exc}"
        return ToolExecutionResult(
            name=call.name,
            ok=False,
            text=text,
            llm_text=text,
            structured={
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            },
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )
