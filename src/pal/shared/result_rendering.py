from __future__ import annotations

import json
from typing import Any


def render_structured_for_llm(structured: Any, *, fallback_text: str = "") -> str:
    if structured is None:
        return str(fallback_text or "").strip()
    if isinstance(structured, str):
        return structured.strip()
    try:
        return json.dumps(structured, ensure_ascii=False, sort_keys=True, indent=2)
    except TypeError:
        return str(structured)


def render_titled_structured_for_llm(title: str, structured: Any, *, fallback_text: str = "") -> str:
    body = render_structured_for_llm(structured, fallback_text=fallback_text)
    normalized_title = str(title or "").strip()
    if not normalized_title:
        return body
    if not body:
        return normalized_title
    return f"{normalized_title}:\n{body}"
