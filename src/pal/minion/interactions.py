from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from pal.control.contracts import ControlDelivery, ControlRoute, InteractionButtonSpec, InteractionMessageSpec
from pal.control.interactions import delivery_for_interaction

_MINION_APPROVAL_ARG_LIMIT = 320
_TELEGRAM_MESSAGE_TEXT_LIMIT = 4096
_INTERACTION_MESSAGE_SAFE_LIMIT = min(3900, _TELEGRAM_MESSAGE_TEXT_LIMIT)
_MINION_APPROVAL_TEXT_LIMIT = _INTERACTION_MESSAGE_SAFE_LIMIT
_MINION_QUESTION_BUTTON_LABEL_LIMIT = 48
_MINION_QUESTION_EVIDENCE_ITEM_LIMIT = 900


def minion_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery:
    return delivery_for_interaction(
        route,
        "interactive_open",
        build_minion_approval_interaction(payload, route),
    )


def build_minion_approval_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec:
    approval_id = str(payload.get("approval_id") or f"approval_{uuid4().hex[:8]}")
    action_payload = {**payload, "approval_id": approval_id}
    return InteractionMessageSpec(
        interaction_id=approval_id,
        interaction_kind="approval_request",
        route=route,
        text=render_minion_approval_text(action_payload),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(action_payload, "accept"),
                ),
                InteractionButtonSpec(
                    label="Accept All",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(action_payload, "accept_all"),
                ),
            ),
            (
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(action_payload, "reject"),
                ),
            ),
        ),
    )


def render_minion_approval_text(payload: dict[str, Any]) -> str:
    args_summary = payload.get("args_summary")
    if not isinstance(args_summary, dict):
        args_summary = {}
    lines = [
        "Minion approval request",
        "",
        f"Title: {payload.get('title') or 'High-risk operation'}",
        f"Run: {payload.get('run_id') or '-'}",
        f"Work order: {payload.get('work_order_id') or '-'}",
        f"Requested action: {payload.get('requested_action') or payload.get('target') or '-'}",
        f"Risk: {payload.get('risk') or 'high'}",
    ]
    impact = str(payload.get("impact") or "").strip()
    if impact:
        lines.append(f"Impact: {impact}")
    target = str(payload.get("target") or "").strip()
    if target:
        lines.append(f"Target: {target}")
    rendered_args = _render_minion_approval_args(args_summary)
    if rendered_args:
        lines.append("Args:")
        lines.extend(rendered_args)
    return _truncate_text("\n".join(lines), _MINION_APPROVAL_TEXT_LIMIT)


def minion_lesson_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_lesson_approval_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_module_dag_tick_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_module_dag_tick_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_plan_acceptance_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_plan_acceptance_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def build_minion_plan_acceptance_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    plan_ref = payload.get("plan_ref")
    if not isinstance(plan_ref, dict):
        return None
    work_order_id = str(payload.get("work_order_id") or "").strip()
    plan_id = str(plan_ref.get("plan_id") or "").strip()
    target_id = work_order_id or plan_id
    if not target_id:
        return None
    review_gate_ref = payload.get("review_gate_ref")
    if not isinstance(review_gate_ref, dict):
        review_gate_ref = {}
    interaction_id = f"minion_plan_accept_{target_id}"
    summary = str(payload.get("summary") or "").strip()
    lines = [
        "Minion plan review passed.",
        "",
        f"Work order: {work_order_id or '-'}",
        f"Plan: {plan_id or '-'}",
    ]
    revision = plan_ref.get("plan_revision")
    if revision is not None:
        lines.append(f"Revision: {revision}")
    gate_id = str(review_gate_ref.get("gate_id") or "").strip()
    if gate_id:
        lines.append(f"Review gate: {gate_id}")
    if summary:
        lines.extend(["", "Summary:", _truncate_text(summary, 900)])
    lines.append("")
    lines.append("Accept this reviewed plan for dispatch?")
    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_plan_acceptance",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept Plan",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_plan_accept_override",
                        target_id=target_id,
                        args={
                            "work_order_id": work_order_id,
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(review_gate_ref),
                            "reason": "human accepted reviewer-passed plan from channel interaction",
                        },
                    ),
                ),
                InteractionButtonSpec(
                    label="Reject Plan",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_plan_reject",
                        target_id=target_id,
                        args={
                            "work_order_id": work_order_id,
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(review_gate_ref),
                            "reason": "human rejected reviewer-passed plan from channel interaction",
                        },
                    ),
                ),
                InteractionButtonSpec(
                    label="Edit Plan",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_plan_edit",
                        target_id=target_id,
                        args={
                            "work_order_id": work_order_id,
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(review_gate_ref),
                            "reason": "human requested edits for reviewer-passed plan from channel interaction",
                        },
                    ),
                ),
            ),
        ),
    )


def build_minion_module_dag_tick_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    if not bool(payload.get("has_next_module")):
        return None
    work_order_id = str(payload.get("parent_work_order_id") or payload.get("work_order_id") or "").strip()
    if not work_order_id:
        return None
    module_id = str(payload.get("module_id") or "").strip()
    next_module_id = str(payload.get("next_module_id") or "").strip()
    milestone_index = payload.get("parent_milestone_index")
    interaction_id = f"minion_dag_tick_{work_order_id}"
    lines = [
        "Minion milestone completed.",
        "",
        f"Work order: {work_order_id}",
    ]
    if module_id:
        lines.append(f"Completed module: {module_id}")
    if milestone_index is not None:
        lines.append(f"Completed milestone: {milestone_index}")
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.extend(["", "Summary:", _truncate_text(summary, 900)])
    if next_module_id:
        lines.extend(["", f"Next module: {next_module_id}"])
    lines.append("")
    lines.append("Tick the DAG to start the next ready module?")

    def action_args(action_kind: str, *, reason: str = "") -> dict[str, Any]:
        return {
            "action_kind": action_kind,
            "target_scope": "minion",
            "target_id": work_order_id,
            "args": {
                "work_order_id": work_order_id,
                "module_id": module_id,
                "next_module_id": next_module_id,
                "parent_milestone_index": milestone_index,
                "reason": reason,
            },
        }

    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_module_dag_tick",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Tick DAG",
                    action_key="control.action.dispatch",
                    action_args=action_args("minion_dag_tick"),
                ),
                InteractionButtonSpec(
                    label="Pause",
                    action_key="control.action.dispatch",
                    action_args=action_args("minion_plan_pause", reason="user paused at module milestone boundary"),
                ),
            ),
            (
                InteractionButtonSpec(
                    label="Finish",
                    action_key="control.action.dispatch",
                    action_args=action_args("minion_plan_finish", reason="user finished at module milestone boundary"),
                ),
            ),
        ),
    )


def build_minion_lesson_approval_interaction(
    payload: dict[str, Any],
    route: ControlRoute,
) -> InteractionMessageSpec | None:
    task_lessons = _string_list(payload.get("task_lessons"))
    system_lessons = _string_list(payload.get("system_lessons"))
    if not task_lessons and not system_lessons:
        return None
    run_id = str(payload.get("run_id") or "")
    work_order_id = str(payload.get("work_order_id") or "")
    interaction_id = f"minion_lesson_{run_id or uuid4().hex[:12]}"
    lines = [
        "Minion proposed reusable lessons.",
        "",
        "Absorb these into Pal memory?",
    ]
    if task_lessons:
        lines.append("")
        lines.append("Task lessons:")
        lines.extend(f"- {lesson}" for lesson in task_lessons)
    if system_lessons:
        lines.append("")
        lines.append("System lessons:")
        lines.extend(f"- {lesson}" for lesson in system_lessons)
    base_args = {
        "work_order_id": work_order_id,
        "run_id": run_id,
        "minion_id": str(payload.get("minion_id") or ""),
        "task_lessons": task_lessons,
        "system_lessons": system_lessons,
    }

    def action_args(decision: str) -> dict[str, Any]:
        return {
            "action_kind": "minion_lesson_decision",
            "target_scope": "minion",
            "target_id": work_order_id,
            "args": {**base_args, "decision": decision},
        }

    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_lesson_approval",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args=action_args("accept"),
                ),
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args=action_args("reject"),
                ),
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args=action_args("edit"),
                ),
            ),
        ),
    )


def minion_question_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_question_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_question_update_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_question_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_update", interaction)


def minion_question_resolve_delivery(payload: dict[str, Any], route: ControlRoute, text: str) -> ControlDelivery | None:
    session = minion_question_session(payload)
    interaction_id = str(session.get("interaction_id") or "").strip()
    if not interaction_id:
        return None
    interaction = InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_question",
        route=route,
        text=_truncate_text(text, _INTERACTION_MESSAGE_SAFE_LIMIT),
        buttons=(),
    )
    return delivery_for_interaction(route, "interactive_resolve", interaction)


def build_minion_question_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    session = minion_question_session(payload)
    questions = _dict_list(session.get("questions"))[:3]
    if not questions:
        return None
    if minion_question_ready(session) and bool(session.get("review")):
        return _build_minion_question_review_interaction(session, route)
    current_index = _clamp_int(session.get("current_index"), minimum=0, maximum=len(questions) - 1)
    current_question = dict(questions[current_index])
    answers = _answer_map(session.get("answers"))
    required_ids = _required_question_ids(questions)
    answered_required = sum(1 for question_id in required_ids if question_id in answers)
    question_id = _question_id(current_question, current_index)
    selected_answer = answers.get(question_id, {})
    lines = [
        "Architect needs input.",
        "",
        f"Question {current_index + 1}/{len(questions)}",
    ]
    question_text = str(current_question.get("question") or "").strip()
    if question_text:
        lines.append(question_text)
    why_needed = str(current_question.get("why_needed") or "").strip()
    if why_needed:
        lines.extend(["", f"Why: {why_needed}"])
    evidence = _question_evidence_preview(current_question.get("evidence"))
    if evidence:
        lines.append("")
        lines.append("Evidence:")
        lines.extend(f"- {item}" for item in evidence)
    options = _dict_list(current_question.get("options"))[:3]
    selected_option_id = str(selected_answer.get("selected_option_id") or "").strip()
    if options:
        lines.append("")
        lines.append("Options:")
        for option_index, option in enumerate(options, start=1):
            option_id = _option_id(option, option_index)
            full_label = _option_label(option, option_index)
            description = str(option.get("description") or option.get("summary") or "").strip()
            option_text = f"{full_label}: {description}" if description and description != full_label else full_label
            marker = ">" if option_id == selected_option_id else " "
            lines.append(f"{marker} {option_index}. {option_text}")
    lines.append("")
    lines.append(f"Answered: {answered_required}/{len(required_ids)}")

    rows: list[tuple[InteractionButtonSpec, ...]] = []
    option_buttons: list[InteractionButtonSpec] = []
    for option_index, option in enumerate(options, start=1):
        option_id = _option_id(option, option_index)
        full_label = _option_label(option, option_index)
        selected = option_id == selected_option_id
        option_buttons.append(
            InteractionButtonSpec(
                label=_question_option_button_label(full_label, selected=selected),
                action_key="control.action.dispatch",
                action_args=_minion_question_action_payload(
                    session,
                    action_kind="minion_question_select",
                    current_index=current_index,
                    extra={
                        "question_id": question_id,
                        "selected_option_id": option_id,
                        "answer": full_label,
                    },
                ),
            )
        )
    if option_buttons:
        rows.append(tuple(option_buttons))
    nav_buttons: list[InteractionButtonSpec] = []
    if current_index > 0:
        nav_buttons.append(
            InteractionButtonSpec(
                label="Back",
                action_key="control.action.dispatch",
                action_args=_minion_question_action_payload(
                    session,
                    action_kind="minion_question_nav",
                    current_index=current_index,
                    extra={"target_index": current_index - 1},
                ),
            )
        )
    if current_index < len(questions) - 1:
        nav_buttons.append(
            InteractionButtonSpec(
                label="Next",
                action_key="control.action.dispatch",
                action_args=_minion_question_action_payload(
                    session,
                    action_kind="minion_question_nav",
                    current_index=current_index,
                    extra={"target_index": current_index + 1},
                ),
            )
        )
    if nav_buttons:
        rows.append(tuple(nav_buttons))
    if not rows:
        return None
    return InteractionMessageSpec(
        interaction_id=str(session.get("interaction_id") or ""),
        interaction_kind="minion_question",
        route=route,
        text=_truncate_text("\n".join(lines), _INTERACTION_MESSAGE_SAFE_LIMIT),
        buttons=tuple(rows),
    )


def minion_question_session(payload: dict[str, Any]) -> dict[str, Any]:
    raw = dict(payload or {})
    question_payload = raw.get("ask_user_question")
    if isinstance(question_payload, dict):
        raw = {**raw, **dict(question_payload)}
    run_id = str(raw.get("run_id") or "").strip()
    clarification_id = str(raw.get("clarification_id") or raw.get("approval_id") or "").strip()
    if not clarification_id:
        clarification_id = f"clarify_{run_id or uuid4().hex[:12]}"
    questions = _dict_list(raw.get("questions"))[:3]
    answers = _answer_map(raw.get("answers"))
    return {
        "clarification_id": clarification_id,
        "interaction_id": str(raw.get("interaction_id") or f"minion_question_{clarification_id}"),
        "run_id": run_id,
        "minion_id": str(raw.get("minion_id") or "").strip(),
        "work_order_id": str(raw.get("work_order_id") or "").strip(),
        "task_id": str(raw.get("task_id") or "").strip(),
        "turn_index": raw.get("turn_index", 0),
        "plan_revision": raw.get("plan_revision", 0),
        "plan_draft_id": str(raw.get("plan_draft_id") or "").strip(),
        "questions": questions,
        "answers": answers,
        "current_index": _clamp_int(raw.get("current_index"), minimum=0, maximum=max(len(questions) - 1, 0)),
        "review": bool(raw.get("review")),
    }


def minion_question_session_with_selection(session: dict[str, Any], *, question_id: str, selected_option_id: str, answer: str) -> dict[str, Any]:
    updated = dict(session or {})
    answers = _answer_map(updated.get("answers"))
    normalized_question_id = str(question_id or "").strip()
    if normalized_question_id:
        answers[normalized_question_id] = {
            "question_id": normalized_question_id,
            "selected_option_id": str(selected_option_id or "").strip(),
            "answer": str(answer or "").strip(),
            "run_id": str(updated.get("run_id") or ""),
            "minion_id": str(updated.get("minion_id") or ""),
            "turn_index": updated.get("turn_index", 0),
            "plan_revision": updated.get("plan_revision", 0),
        }
    updated["answers"] = answers
    questions = _dict_list(updated.get("questions"))[:3]
    required_ids = _required_question_ids(questions)
    if all(question_id in answers for question_id in required_ids):
        updated["ready"] = True
        updated["review"] = True
        return updated
    current_index = _clamp_int(updated.get("current_index"), minimum=0, maximum=max(len(questions) - 1, 0))
    for index in range(current_index + 1, len(questions)):
        if _question_id(questions[index], index) in required_ids and _question_id(questions[index], index) not in answers:
            updated["current_index"] = index
            return updated
    for index, question in enumerate(questions):
        if _question_id(question, index) in required_ids and _question_id(question, index) not in answers:
            updated["current_index"] = index
            return updated
    return updated


def minion_question_answers(session: dict[str, Any]) -> list[dict[str, Any]]:
    answers = _answer_map((session or {}).get("answers"))
    questions = _dict_list((session or {}).get("questions"))[:3]
    result: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        question_id = _question_id(question, index)
        answer = dict(answers.get(question_id) or {})
        if not answer:
            continue
        answer.setdefault("question_id", question_id)
        answer.setdefault("question", str(question.get("question") or "").strip())
        result.append(answer)
    return result


def minion_question_ready(session: dict[str, Any]) -> bool:
    questions = _dict_list((session or {}).get("questions"))[:3]
    answers = _answer_map((session or {}).get("answers"))
    required_ids = _required_question_ids(questions)
    return bool(required_ids) and all(question_id in answers for question_id in required_ids)


def _minion_approval_action_payload(payload: dict[str, Any], decision: str) -> dict[str, Any]:
    approval_id = str(payload.get("approval_id") or "")
    return _minion_interaction_action_payload(
        action_kind="minion_approval_decision",
        target_id=approval_id,
        args={
            "approval_id": approval_id,
            "run_id": str(payload.get("run_id") or ""),
            "minion_id": str(payload.get("minion_id") or ""),
            "decision": decision,
        },
    )


def _minion_interaction_action_payload(*, action_kind: str, target_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_kind": action_kind,
        "target_scope": "minion",
        "target_id": str(target_id or ""),
        "args": dict(args),
    }


def _render_minion_approval_args(args_summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for index, (key, value) in enumerate(sorted(args_summary.items())):
        if index >= 8:
            lines.append(f"- ... {len(args_summary) - index} more args")
            break
        rendered = _render_arg_value(value)
        lines.append(f"- {key}: {_truncate_text(rendered, _MINION_APPROVAL_ARG_LIMIT)}")
    return lines


def _render_arg_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return " ".join(str(value or "").split())


def _truncate_text(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 24)].rstrip()}... [truncated]"


def _question_evidence_preview(value: Any) -> list[str]:
    if isinstance(value, str):
        rendered = " ".join(value.split())
        return [_truncate_text(rendered, _MINION_QUESTION_EVIDENCE_ITEM_LIMIT)] if rendered else []
    items = _dict_list(value)
    if not items:
        return []
    rendered_items: list[str] = []
    for item in items:
        file_name = str(item.get("file") or item.get("path") or "").strip()
        finding = str(item.get("finding") or item.get("summary") or item.get("text") or "").strip()
        rendered = f"{file_name}: {finding}" if file_name and finding else file_name or finding
        rendered = " ".join(rendered.split())
        if rendered:
            rendered_items.append(_truncate_text(rendered, _MINION_QUESTION_EVIDENCE_ITEM_LIMIT))
    return rendered_items


def _question_option_button_label(label: str, *, selected: bool) -> str:
    prefix = "> " if selected else ""
    text = " ".join(str(label or "").split()) or "Option"
    limit = max(8, _MINION_QUESTION_BUTTON_LABEL_LIMIT - len(prefix))
    if len(text) > limit:
        text = f"{text[: max(0, limit - 3)].rstrip()}..."
    return f"{prefix}{text}"


def _build_minion_question_review_interaction(session: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    questions = _dict_list(session.get("questions"))[:3]
    answers = _answer_map(session.get("answers"))
    if not questions:
        return None
    lines = [
        "Architect needs input.",
        "",
        "Review answers",
    ]
    for index, question in enumerate(questions):
        question_id = _question_id(question, index)
        answer = answers.get(question_id, {})
        question_text = " ".join(str(question.get("question") or f"Question {index + 1}").split())
        answer_text = _question_answer_text(question, answer)
        lines.append("")
        lines.append(f"{index + 1}. {question_text}")
        lines.append(f"> {answer_text or '-'}")
    edit_args = _minion_question_action_payload(
        session,
        action_kind="minion_question_nav",
        current_index=0,
        extra={"target_index": 0, "review": False},
    )
    submit_args = _minion_question_action_payload(
        session,
        action_kind="minion_question_submit",
        current_index=_clamp_int(session.get("current_index"), minimum=0, maximum=max(len(questions) - 1, 0)),
    )
    return InteractionMessageSpec(
        interaction_id=str(session.get("interaction_id") or ""),
        interaction_kind="minion_question",
        route=route,
        text=_truncate_text("\n".join(lines), _INTERACTION_MESSAGE_SAFE_LIMIT),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args=edit_args,
                ),
                InteractionButtonSpec(
                    label="Submit",
                    action_key="control.action.dispatch",
                    action_args=submit_args,
                ),
            ),
        ),
    )


def _question_answer_text(question: dict[str, Any], answer: dict[str, Any]) -> str:
    explicit = str(answer.get("answer") or "").strip()
    if explicit:
        return explicit
    selected = str(answer.get("selected_option_id") or "").strip()
    for index, option in enumerate(_dict_list(question.get("options"))[:3], start=1):
        if _option_id(option, index) == selected:
            return _option_label(option, index)
    return selected


def _minion_question_action_payload(
    session: dict[str, Any],
    *,
    action_kind: str,
    current_index: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = {
        "clarification_id": str(session.get("clarification_id") or ""),
        "interaction_id": str(session.get("interaction_id") or ""),
        "work_order_id": str(session.get("work_order_id") or ""),
        "run_id": str(session.get("run_id") or ""),
        "minion_id": str(session.get("minion_id") or ""),
        "turn_index": session.get("turn_index", 0),
        "plan_revision": session.get("plan_revision", 0),
        "questions": _dict_list(session.get("questions"))[:3],
        "answers": _answer_map(session.get("answers")),
        "current_index": int(current_index),
        "review": bool(session.get("review")),
    }
    args.update(dict(extra or {}))
    return _minion_interaction_action_payload(
        action_kind=action_kind,
        target_id=str(session.get("clarification_id") or ""),
        args=args,
    )


def _answer_map(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        return {str(key): dict(item) for key, item in value.items() if str(key).strip() and isinstance(item, dict)}
    if isinstance(value, (list, tuple)):
        result: dict[str, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            if question_id:
                result[question_id] = dict(item)
        return result
    return {}


def _required_question_ids(questions: list[dict[str, Any]]) -> list[str]:
    return [
        _question_id(question, index)
        for index, question in enumerate(questions)
        if question.get("blocking") is not False
    ]


def _question_id(question: dict[str, Any], index: int) -> str:
    return str(question.get("question_id") or f"q{index + 1}").strip()


def _option_id(option: dict[str, Any], index: int) -> str:
    return str(option.get("id") or option.get("option_id") or f"option_{index}").strip()


def _option_label(option: dict[str, Any], index: int) -> str:
    return str(option.get("label") or _option_id(option, index)).strip() or f"Option {index}"


def _clamp_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return _dedupe_nonempty([str(item) for item in values])


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
