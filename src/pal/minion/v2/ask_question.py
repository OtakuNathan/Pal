from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pal.execution.generated_tool_models import (
    MinionV2AskQuestionInput,
)
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.shared import RuntimeStatus


ASK_QUESTION_CAPABILITY = "op_minion_ask_question"

ASK_QUESTION_TOOL_SPEC: dict[str, Any] = {
    "alias": "ask_question",
    "description": (
        "Ask one decisive user question when the task contains a contradiction, "
        "material ambiguity, infeasible or incorrect requirement, missing "
        "preference, or a decision that changes product behavior, compatibility, "
        "architecture, modification scope, or implementation scope. Do not guess "
        "or silently reinterpret the task. Supply a short title, a precise "
        "question, and three choice strings that include their impact/tradeoff; "
        "the channel also permits free text. The logical role invocation suspends "
        "without a wall-clock deadline. Before the answer returns, Manager appends "
        "the exact exchange to immutable task.yaml; continue directly and never "
        "edit or restate the task ledger."
    ),
    "InputModel": MinionV2AskQuestionInput,
}


async def ask_question_tool_result(
    call: CanonicalToolCall,
    *,
    request_user: (
        Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None
    ),
) -> CanonicalToolResult:
    if request_user is None:
        return CanonicalToolResult(
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
            return CanonicalToolResult(
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
        return CanonicalToolResult(
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
        return CanonicalToolResult(
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
