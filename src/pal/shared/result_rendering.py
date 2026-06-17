from __future__ import annotations

import json
from typing import Any


_PRETTY_JSON_CHAR_LIMIT = 8_000


def render_structured_for_llm(structured: Any, *, fallback_text: str = "") -> str:
    if structured is None:
        return str(fallback_text or "").strip()
    if isinstance(structured, str):
        return structured.strip()
    try:
        compact = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        pretty = json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True)
        if len(pretty) <= _PRETTY_JSON_CHAR_LIMIT:
            return pretty
        return compact
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


def render_head_tail_preview_for_llm(text: str, *, max_chars: int) -> tuple[str, int]:
    source = str(text or "")
    if max_chars <= 0:
        return "", 0
    if len(source) <= max_chars:
        return source.strip(), len(source)
    if max_chars < 512:
        preview = source[:max_chars].rstrip()
        return preview, len(preview)

    head_chars = max(256, max_chars // 2)
    tail_chars = max_chars - head_chars
    if tail_chars < 256:
        tail_chars = 256
        head_chars = max(1, max_chars - tail_chars)

    head = source[:head_chars].rstrip()
    tail = source[-tail_chars:].lstrip()
    omitted_chars = max(0, len(source) - head_chars - tail_chars)
    preview = "\n".join(
        part
        for part in (
            "--- head ---",
            head,
            f"--- omitted {omitted_chars} chars ---",
            "--- tail ---",
            tail,
        )
        if part
    ).strip()
    return preview, len(head) + len(tail)
