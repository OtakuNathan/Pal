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
        for row in list(raw.get("buttons") or []):
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
            text=str(raw.get("text") or ""),
            options=tuple(options),
        )
