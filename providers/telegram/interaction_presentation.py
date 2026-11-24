"""Telegram presentation only; interaction actions remain owned by their producer."""
from __future__ import annotations

import hashlib

from pal.control.contracts import InteractionMessageSpec
from pal.control.presentation import interaction_button_rows


SEPARATE_DETAILS_KINDS = frozenset({
    "approval_request", "bunshin_approval", "bunshin_clarification",
    "bunshin_architecture_review",
    "bunshin_v2_architecture_review",
})


def decision_text(spec: InteractionMessageSpec) -> str:
    title = next((line.strip().lstrip("# ") for line in spec.text.splitlines() if line.strip()), "Choose an option")
    if len(title) > 160:
        title = title[:159] + "…"
    lines = [title, "", "Read the supplied details, then choose an option."]
    if spec.items:
        lines.extend(f"{item.title} [{item.state}]" for item in spec.items)
    else:
        buttons = [button for row in interaction_button_rows(spec) for button in row]
        lines.extend(
            f"{chr(65 + index)}: {button['label'][:160]}" if index < 26 else button["label"][:160]
            for index, button in enumerate(buttons)
        )
    return "\n".join(lines)


def closed_text(spec: InteractionMessageSpec, target: dict) -> str:
    # Some producers append the decision after the original details. Remove
    # only a byte-identical delivered prefix, preserving the complete decision.
    raw = spec.text.encode("utf-8")
    size = target.get("details_bytes", 0)
    if (type(size) is int and size > 0 and len(raw) >= size
            and hashlib.sha256(raw[:size]).hexdigest() == target.get("details_digest")):
        return raw[size:].decode("utf-8").strip() or "This interaction is closed."
    return spec.text or "This interaction is closed."
