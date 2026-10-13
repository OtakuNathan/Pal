"""LLM projections of Pal-owned discovery metadata, never of user data.

Full schemas and index records remain available to validation and IPC callers.
Do not recursively filter arbitrary tool results by these field names.
"""
from __future__ import annotations

from typing import Any

from pal.shared.result_rendering import render_structured_for_llm


def compact_input_contract(schema: Any) -> Any:
    """Remove schema titles, not data or validation rules (including MCP rules).

    Only traverse JSON Schema positions. A property named 'title', an enum
    object, or a default value is user data and must survive unchanged.
    """
    if not isinstance(schema, dict):
        return schema
    maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    singles = {"items", "additionalItems", "additionalProperties", "unevaluatedProperties",
               "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else", "contentSchema"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    result = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key in maps and isinstance(value, dict):
            value = {name: compact_input_contract(child) for name, child in value.items()}
        elif key in singles:
            value = ([compact_input_contract(child) for child in value]
                     if isinstance(value, list) else compact_input_contract(value))
        elif key in arrays and isinstance(value, list):
            value = [compact_input_contract(child) for child in value]
        elif key == "dependencies" and isinstance(value, dict):
            value = {name: compact_input_contract(child) for name, child in value.items()}
        result[key] = value
    return result


def native_input_contract(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local object references for native functions without changing rules.

    Keep definitions for recursive references and leave opaque enum/default data
    untouched. Some model endpoints misread a referenced object as a string.
    """
    maps = {"properties", "patternProperties", "dependentSchemas"}
    singles = {"items", "additionalItems", "additionalProperties", "unevaluatedProperties",
               "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else", "contentSchema"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}

    def visit(node: Any, seen: frozenset[str] = frozenset()) -> Any:
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/") and reference not in seen:
            target: Any = schema
            for part in reference[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part) if isinstance(target, dict) else None
            if isinstance(target, dict):
                node = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
                seen = seen | {reference}
        result = {}
        for key, value in node.items():
            if key in maps and isinstance(value, dict):
                value = {name: visit(child, seen) for name, child in value.items()}
            elif key in singles:
                value = ([visit(child, seen) for child in value] if isinstance(value, list) else visit(value, seen))
            elif key in arrays and isinstance(value, list):
                value = [visit(child, seen) for child in value]
            elif key == "dependencies" and isinstance(value, dict):
                value = {name: visit(child, seen) for name, child in value.items()}
            result[key] = value
        return result

    return visit(schema)


def render_tool_definition(payload: dict[str, Any], *, view: str = "input") -> str:
    excluded = (set() if view == "full" else
                {"input_schema", "example"} if view == "output" else
                {"output_schema", "example"})
    return render_structured_for_llm({
        key: value for key, value in payload.items()
        if key not in excluded
    })


def render_tool_inventory(payload: dict[str, Any]) -> str:
    return render_structured_for_llm({
        **payload,
        "tools": [{key: value for key, value in tool.items()
                   if key in {"name", "purpose", "module", "invocation_mode"}}
                  for tool in payload["tools"]],
    })


def render_tool_search(generation: Any, payload: dict[str, Any]) -> str:
    """Deliver callable contracts without repeating the index or field list."""
    _ = generation
    hits = [{key: value for key, value in hit.items()
             if key not in {"score", "search_text", "input_shape", "search_objects", "search_terms", "search_auxiliary_terms", "search_enum_fields", "search_term_rules", "search_scope_terms", "search_intents"}}
            for hit in payload.get("hits", ())]
    suggestions = [{**{key: value for key, value in suggestion.items() if key not in {'score', 'retrieval_sources'}}, **({'contract': {
        key: value for key, value in suggestion['contract'].items()
        if key not in {'score', 'search_text', 'input_shape', 'search_auxiliary_terms'}
    }} if suggestion.get('contract') is not None else {})}
        for suggestion in payload.get('suggestions', ())]
    result = {**payload, 'hits': hits}
    if 'suggestions' in payload:
        result['suggestions'] = suggestions
    return render_structured_for_llm(result)
