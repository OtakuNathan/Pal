from __future__ import annotations

from typing import Any
from uuid import uuid4

from pal.control.contracts import ControlDelivery, ControlRoute, InteractionButtonSpec, InteractionMessageSpec
from pal.control.interactions import delivery_for_interaction

_MEMORY_CANDIDATE_PREVIEW_LIMIT = 1000


def memory_candidate_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_memory_candidate_approval_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def build_memory_candidate_approval_interaction(
    payload: dict[str, Any],
    route: ControlRoute,
) -> InteractionMessageSpec | None:
    memory_candidates = _dict_list(payload.get("memory_candidates"))
    if not memory_candidates:
        return None
    source_kind = str(payload.get("source_kind") or "pal").strip() or "pal"
    source_ref = str(payload.get("source_ref") or payload.get("work_order_id") or payload.get("run_id") or "").strip()
    source_label = str(payload.get("source_label") or "").strip()
    candidate_batch_id = str(payload.get("candidate_batch_id") or source_ref or uuid4().hex[:12]).strip()
    interaction_id = f"memory_candidate_{candidate_batch_id}"
    source_name = str(payload.get("source_name") or source_kind.replace("_", " ").title() or "Pal").strip() or "Pal"
    lines = [
        f"{source_name} proposed durable memory candidates.",
        "",
        "Save these into Pal memory?",
    ]
    if source_label:
        lines.extend(["", f"Source: {source_label}"])
    elif source_ref:
        lines.extend(["", f"Source: {source_kind}:{source_ref}"])
    lines.append("")
    lines.append("Memory candidates:")
    lines.extend(f"- {_memory_candidate_preview(candidate)}" for candidate in memory_candidates)
    base_args = {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_label": source_label,
        "memory_candidates": memory_candidates,
    }

    def action_args(decision: str) -> dict[str, Any]:
        return {
            "action_kind": "memory_candidate_decision",
            "target_scope": "memory",
            "target_id": candidate_batch_id,
            "args": {**base_args, "decision": decision},
        }

    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="memory_candidate_approval",
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


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _memory_candidate_preview(candidate: dict[str, Any]) -> str:
    title = " ".join(str(candidate.get("title") or "").split())
    summary = " ".join(str(candidate.get("summary") or candidate.get("search_text") or "").split())
    if title and summary and summary != title:
        text = f"{title}: {summary}"
    else:
        text = title or summary or "Untitled memory candidate"
    if len(text) > _MEMORY_CANDIDATE_PREVIEW_LIMIT:
        return f"{text[: _MEMORY_CANDIDATE_PREVIEW_LIMIT - 3].rstrip()}..."
    return text
