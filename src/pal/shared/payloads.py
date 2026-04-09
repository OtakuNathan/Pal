from __future__ import annotations

from typing import Any


def extract_text_from_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        direct = payload.get("text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        normalized = payload.get("normalized")
        if normalized is not None:
            nested = extract_text_from_payload(normalized)
            if nested:
                return nested
    return ""
