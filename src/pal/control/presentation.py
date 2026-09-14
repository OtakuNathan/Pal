"""Provider-neutral interaction projection. Action authority stays on the host."""
from __future__ import annotations

import hashlib

from pal.control.contracts import InteractionMessageSpec


def interaction_projection(spec: InteractionMessageSpec) -> tuple[dict, dict]:
    actions: dict = {}
    prefix = "r" + hashlib.sha256(spec.revision.encode()).hexdigest()[:12] if spec.revision else ""

    def button(value, *, inputs=()):
        token = f"{prefix}b{len(actions)}"
        actions[token] = {
            "action_key": value.action_key,
            "action_args": dict(value.action_args),
            "input_ids": list(inputs),
        }
        return {"label": value.label, "token": token}

    rows = [[button(item) for item in row] for row in spec.buttons]
    items = []
    for item in spec.items:
        items.append({
            "item_id": item.item_id, "title": item.title, "text": item.text,
            "state": item.state,
            "buttons": [[button(value) for value in row] for row in item.buttons],
        })
    inputs = [{
        "input_id": item.input_id, "label": item.label, "value": item.value,
        "multiline": item.multiline, "submit": button(item.submit, inputs=(item.input_id,)),
    } for item in spec.inputs]
    return {
        "interaction_id": spec.interaction_id, "interaction_kind": spec.interaction_kind,
        "text": spec.text, "buttons": rows, "expires_at": spec.expires_at,
        "revision": spec.revision, "items": items, "inputs": inputs,
    }, actions


def interaction_text(spec: InteractionMessageSpec) -> str:
    parts = [spec.text]
    for item in spec.items:
        parts.append(f"{item.title} [{item.state}]\n{item.text}")
    return "\n\n".join(part for part in parts if part)


def interaction_button_rows(spec: InteractionMessageSpec) -> list[list[dict]]:
    projection, _ = interaction_projection(spec)
    return [*projection["buttons"], *[row for item in projection["items"] for row in item["buttons"]]]
