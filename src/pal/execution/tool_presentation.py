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
    """Render selection and routing guidance without repeating index fields."""
    from pal.execution.tool_registry import _compile_next_tool_lines

    hits = []
    for hit in payload["hits"]:
        record = generation.record_for_alias(hit["alias"])
        item = {
            "alias": hit["alias"],
            "purpose": generation.project_llm_text(record.guidance.purpose),
            "use_when": generation.project_llm_text(record.guidance.use_when),
            "invocation_mode": hit["invocation_mode"],
            "input_shape": record.compact_input_shape(),
        }
        next_tools = _compile_next_tool_lines(
            record, direct_aliases=generation.direct_aliases,
            indirect_aliases=generation.indirect_aliases,
        )
        if next_tools:
            item["next_tools"] = generation.project_llm_value(next_tools)
        hits.append(item)
    return render_structured_for_llm({
        **{key: payload[key] for key in (
            "total_count", "truncated", "applied_filters", "facets", "usage_hint"
        ) if key in payload},
        "hits": hits,
    })
