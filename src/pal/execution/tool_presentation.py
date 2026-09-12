"""LLM projections of Pal-owned discovery metadata, never of user data.

Full schemas and index records remain available to validation and IPC callers.
Do not recursively filter arbitrary tool results by these field names.
"""
from __future__ import annotations

from typing import Any

from pal.shared.result_rendering import render_structured_for_llm


def render_tool_definition(payload: dict[str, Any]) -> str:
    return render_structured_for_llm({
        key: value for key, value in payload.items()
        if key not in {"output_schema", "example"}
    })


def render_tool_inventory(payload: dict[str, Any]) -> str:
    return render_structured_for_llm({
        **payload,
        "tools": [{key: value for key, value in tool.items() if key != "output_schema"}
                  for tool in payload["tools"]],
    })


def render_tool_search(generation: Any, payload: dict[str, Any]) -> str:
    """Keep baseline search presentation intact except for JSON whitespace."""
    return render_structured_for_llm(payload)
