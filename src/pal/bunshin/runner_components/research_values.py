from __future__ import annotations
from typing import Any


def _web_research_capability_name(name: object) -> str | None:
    normalized = str(name or "").strip()
    if normalized in {"op_web_search", "search_web"}:
        return "op_web_search"
    if normalized in {"op_browser_read", "browser_read"}:
        return "op_browser_read"
    return None


def _web_research_budget_keys(canonical_name: str) -> tuple[str, ...]:
    if canonical_name == "op_web_search":
        return ("op_web_search", "search_web")
    if canonical_name == "op_browser_read":
        return ("op_browser_read", "browser_read")
    return (canonical_name,)


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number
