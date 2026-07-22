from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4


InteractionEventWriter = Callable[[str, dict[str, Any]], Awaitable[None]]
InteractionReader = Callable[[float | None], Awaitable[dict[str, Any] | None]]
DEFAULT_CLARIFICATION_TIMEOUT_SECONDS = 30 * 24 * 60 * 60


@dataclass
class MinionUserInteractionPort:
    emit_event: InteractionEventWriter
    read_response: InteractionReader
    run_id: str
    minion_id: str
    invocation_id: str
    auto_accept_approvals: bool = False

    def should_request_approval(self, capability_name: str, approval_policy: dict[str, Any]) -> bool:
        if self.auto_accept_approvals:
            return False
        high_risk = {str(item) for item in list((approval_policy or {}).get("high_risk_capabilities") or [])}
        return str(capability_name) in high_risk

    async def request_approval(
        self,
        *,
        capability_name: str,
        args_summary: dict[str, Any],
        approval_policy: dict[str, Any],
        approval_kind: str = "high_risk",
        title: str | None = None,
        risk: str = "high",
        impact: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        approval_id = f"appr_{uuid4().hex[:16]}"
        event_metadata = {"approval_kind": str(approval_kind or "approval")}
        event_metadata.update(dict(metadata or {}))
        await self.emit_event(
            "approval_requested",
            {
                "approval_id": approval_id,
                "title": title or "Minion high-risk operation",
                "requested_action": capability_name,
                "risk": risk,
                "impact": impact or "Minion requested permission before running a high-risk operation.",
                "target": capability_name,
                "args_summary": dict(args_summary),
                "metadata": event_metadata,
            },
        )
        timeout = float((approval_policy or {}).get("decision_timeout_seconds") or 300)
        response = await self.read_response(timeout)
        decision = str(((response or {}).get("decision") or {}).get("decision") or "").strip().lower()
        if decision == "accept_all":
            self.auto_accept_approvals = True
        await self.emit_event("decision_received", {"approval_id": approval_id, "decision": decision or "timeout"})
        return "accept" if decision == "accept_all" else decision

    async def request_clarification(
        self,
        ask_user_question: dict[str, Any],
        *,
        approval_policy: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        clarification_id = f"clarify_{uuid4().hex[:16]}"
        payload = {
            **dict(ask_user_question or {}),
            "clarification_id": clarification_id,
            "run_id": self.run_id,
            "minion_id": self.minion_id,
            "invocation_id": self.invocation_id,
            "status": "pending",
        }
        if not clarification_questions_are_interactive(payload.get("questions")):
            await self.emit_event(
                "clarification_unavailable",
                {
                    "clarification_id": clarification_id,
                    "reason": "required clarification questions must include inline options",
                    "summary": ask_user_question_summary(payload),
                },
            )
            return {}
        await self.emit_event("clarification_requested", payload)
        configured_timeout = (approval_policy or {}).get("clarification_timeout_seconds")
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else (
                float(configured_timeout)
                if configured_timeout is not None
                else float(DEFAULT_CLARIFICATION_TIMEOUT_SECONDS)
            )
        )
        response = await self.read_response(timeout)
        if not isinstance(response, dict):
            await self.emit_event("clarification_timeout", {"clarification_id": clarification_id})
            return {}
        clarification = response.get("clarification") if isinstance(response.get("clarification"), dict) else response
        if not isinstance(clarification, dict):
            return {}
        if str(clarification.get("clarification_id") or "") != clarification_id:
            return {}
        await self.emit_event(
            "clarification_received",
            {
                "clarification_id": clarification_id,
                "answer_count": len(list(clarification.get("answers") or [])),
            },
        )
        return dict(clarification)


def ask_user_question_summary(payload: dict[str, Any]) -> str:
    questions = [dict(item) for item in list(payload.get("questions") or []) if isinstance(item, dict)]
    if not questions:
        return "minion asked a user clarification question"
    first = str(questions[0].get("question") or "minion asked a user clarification question").strip()
    if len(questions) == 1:
        return first
    return f"{first} (+{len(questions) - 1} more)"


def clarification_questions_are_interactive(value: Any) -> bool:
    questions = [dict(item) for item in list(value or []) if isinstance(item, dict)]
    if not questions:
        return False
    for question in questions[:3]:
        options = [dict(item) for item in list(question.get("options") or []) if isinstance(item, dict)]
        if not options:
            return False
    return True
