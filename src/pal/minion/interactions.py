from __future__ import annotations

from typing import Any
from uuid import uuid4

from pal.control.contracts import ControlDelivery, ControlRoute, InteractionButtonSpec, InteractionMessageSpec
from pal.control.interactions import delivery_for_interaction


def minion_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery:
    approval_id = str(payload.get("approval_id") or f"approval_{uuid4().hex[:8]}")
    text = "\n".join(
        (
            "Minion approval request",
            "",
            f"Title: {payload.get('title') or 'High-risk operation'}",
            f"Run: {payload.get('run_id') or '-'}",
            f"Action: {payload.get('requested_action') or payload.get('target') or '-'}",
            f"Risk: {payload.get('risk') or 'high'}",
            f"Impact: {payload.get('impact') or '-'}",
        )
    )
    common = {
        "approval_id": approval_id,
        "run_id": str(payload.get("run_id") or ""),
        "minion_id": str(payload.get("minion_id") or ""),
    }
    interaction = InteractionMessageSpec(
        interaction_id=approval_id,
        interaction_kind="minion_approval",
        route=route,
        text=text,
        buttons=(
            (
                _button("Accept", "minion_approval_decision", approval_id, {**common, "decision": "accept"}),
                _button("Accept All", "minion_approval_decision", approval_id, {**common, "decision": "accept_all"}),
                _button("Reject", "minion_approval_decision", approval_id, {**common, "decision": "reject"}),
            ),
        ),
    )
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_question_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    clarification_id = str(payload.get("clarification_id") or f"clarify_{uuid4().hex[:8]}")
    questions = [dict(item) for item in list(payload.get("questions") or []) if isinstance(item, dict)]
    question = questions[0] if questions else {"question": payload.get("question") or payload.get("summary")}
    title = str(question.get("title") or payload.get("title") or "Architecture question").strip()
    descriptions = [
        f"- {option.get('label') or option.get('answer') or f'Option {index}'}: "
        f"{option.get('description') or option.get('label') or option.get('answer') or ''}"
        for index, option in enumerate(
            [dict(item) for item in list(question.get("options") or []) if isinstance(item, dict)][:3],
            start=1,
        )
    ]
    text = "\n".join(
        [
            title,
            "",
            str(question.get("question") or "Minion needs clarification.").strip(),
            "",
            *descriptions,
            "",
            "Choose an option, or reply with a custom answer.",
        ]
    )
    options = [dict(item) for item in list(question.get("options") or []) if isinstance(item, dict)][:3]
    buttons = tuple(
        _button(
            str(option.get("label") or option.get("answer") or f"Option {index}"),
            "minion_question_answer",
            clarification_id,
            {
                "clarification_id": clarification_id,
                "run_id": str(payload.get("run_id") or ""),
                "minion_id": str(payload.get("minion_id") or ""),
                "answers": [
                    {
                        "question_id": str(question.get("question_id") or question.get("id") or "question-1"),
                        "answer": str(option.get("label") or option.get("answer") or ""),
                    }
                ],
            },
        )
        for index, option in enumerate(options, start=1)
    )
    if not buttons:
        return None
    interaction = InteractionMessageSpec(
        interaction_id=clarification_id,
        interaction_kind="minion_clarification",
        route=route,
        text=text,
        buttons=(buttons,),
    )
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_architecture_review_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    workflow_id = str(payload.get("workflow_id") or "")
    revision_id = str(payload.get("architecture_revision_id") or "")
    manifest_sha = str(payload.get("manifest_sha") or "")
    token = str(payload.get("decision_token") or "")
    if not bool(payload.get("minion_v2")) or not all((workflow_id, revision_id, manifest_sha, token)):
        return None
    common = {
        "workflow_id": workflow_id,
        "architecture_revision_id": revision_id,
        "manifest_sha": manifest_sha,
        "decision_token": token,
        "actor_id": str(payload.get("actor_id") or "pal"),
        "active_channel_id": str(payload.get("active_channel_id") or "local"),
    }
    buttons = (
        (
            _button("Accept", "minion_v2_human_decision", workflow_id, {**common, "decision": "accept"}),
            _button("Edit", "minion_v2_human_decision", workflow_id, {**common, "decision": "edit"}),
            _button("Reject", "minion_v2_human_decision", workflow_id, {**common, "decision": "reject"}),
        ),
    )
    interaction = InteractionMessageSpec(
        interaction_id=f"minion_v2_architecture_{revision_id}",
        interaction_kind="minion_v2_architecture_review",
        route=route,
        text=str(payload.get("markdown") or "Architecture review is ready."),
        buttons=buttons,
    )
    return delivery_for_interaction(route, "interactive_open", interaction)


def _button(label: str, action_kind: str, target_id: str, args: dict[str, Any]) -> InteractionButtonSpec:
    return InteractionButtonSpec(
        label=label,
        action_key="control.action.dispatch",
        action_args={
            "action_kind": action_kind,
            "target_scope": "minion",
            "target_id": target_id,
            "args": dict(args),
        },
    )
