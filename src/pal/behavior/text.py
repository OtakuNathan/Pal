from __future__ import annotations

import re


_BULLET_RE = re.compile(r"^\s*[-*\u2022]\s+")


def canonicalize_affordance_prompt_hint(title: object, prompt_hint: object) -> str:
    hint = str(prompt_hint or "").strip()
    title_text = _normalized_title(title)
    if not title_text or not hint:
        return hint
    return strip_affordance_title_prefix(title_text, hint)


def strip_affordance_title_prefix(title: object, text: object) -> str:
    title_text = _normalized_title(title)
    candidate = _BULLET_RE.sub("", str(text or "").strip()).strip()
    if not title_text or not candidate:
        return candidate
    while True:
        stripped = _strip_one_title_prefix(title_text, candidate)
        if stripped == candidate:
            return "" if candidate.casefold() == title_text.casefold() else candidate
        candidate = stripped


def _strip_one_title_prefix(title: str, text: str) -> str:
    for separator in (":", "："):
        prefix = f"{title}{separator}"
        if text.casefold().startswith(prefix.casefold()):
            return text[len(prefix) :].strip()
    return text


def _normalized_title(title: object) -> str:
    return str(title or "").strip().rstrip(":：").strip()
