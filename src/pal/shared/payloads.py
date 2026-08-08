from __future__ import annotations

from typing import Any


def extract_text_from_payload(payload: Any) -> str:
    # Keep command/control consumers independent of whether channel ingress has
    # already compiled the provider payload into L1's message IR.
    try:
        from pal.llm.ir import LLMMessageIR

        if isinstance(payload, LLMMessageIR):
            return payload.text.strip()
    except ImportError:
        pass
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
