"""TTY projection of channel interaction messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TtyInteractionOption:
    label: str
    token: str


@dataclass(frozen=True)
class TtyInteraction:
    interaction_id: str
    interaction_kind: str
    state: str
    text: str
    options: tuple[TtyInteractionOption, ...]
    inputs: tuple[dict[str, Any], ...] = ()
    revision: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TtyInteraction | None:
        state = str(payload.get("type") or "")
        if state not in {
            "interactive_open",
            "interactive_update",
            "interactive_resolve",
            "interactive_expire",
        }:
            return None
        raw = payload.get("interaction")
        if not isinstance(raw, dict):
            return None
        interaction_id = str(raw.get("interaction_id") or "").strip()
        if not interaction_id:
            return None
        options: list[TtyInteractionOption] = []
        items = [item for item in raw.get("items", []) if isinstance(item, dict)]
        rows = [*list(raw.get("buttons") or []), *[row for item in items for row in item.get("buttons", [])]]
        inputs = tuple(item for item in raw.get("inputs", []) if isinstance(item, dict))
        rows.extend([[item["submit"]] for item in inputs if isinstance(item.get("submit"), dict)])
        for row in rows:
            if not isinstance(row, list):
                continue
            for item in row:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                token = str(item.get("token") or "").strip()
                if label and token:
                    options.append(TtyInteractionOption(label=label, token=token))
        return cls(
            interaction_id=interaction_id,
            interaction_kind=str(raw.get("interaction_kind") or ""),
            state=state,
            text="\n\n".join([str(raw.get("text") or ""), *[f'{item.get("title", "")} [{item.get("state", "")}]\n{item.get("text", "")}' for item in items]]),
            inputs=inputs,
            revision=str(raw.get("revision") or ""),
            options=tuple(options),
        )
