"""LLM projections of Pal-owned discovery metadata, never of user data.

Full schemas and index records remain available to validation and IPC callers.
Do not recursively filter arbitrary tool results by these field names.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pal.shared.result_rendering import render_structured_for_llm


def _schema_nodes(schema: Any) -> Iterator[dict[str, Any]]:
    """Walk schema positions, never enum/default/const/example data."""
    if not isinstance(schema, dict):
        return
    yield schema
    for key, value in schema.items():
        if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"} and isinstance(value, dict):
            for child in value.values():
                yield from _schema_nodes(child)
        elif key in {"items", "additionalItems", "additionalProperties", "unevaluatedProperties",
                     "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else", "contentSchema"}:
            for child in value if isinstance(value, list) else [value]:
                yield from _schema_nodes(child)
        elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(value, list):
            for child in value:
                yield from _schema_nodes(child)


def _scoped_references(schema: dict[str, Any]) -> bool:
    return any(node.keys() & {"$id", "id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"}
               for node in _schema_nodes(schema))


def _prune_unused_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """Prune root $defs only when local-pointer reachability is provable.

    Other reference forms/scopes keep the complete definition table. Nested
    definition tables are deliberately not rewritten.
    """
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or _scoped_references(schema):
        return schema
    pending = [node["$ref"] for node in _schema_nodes({k: v for k, v in schema.items() if k != "$defs"})
               if "$ref" in node]
    retained: set[str] = set()
    while pending:
        reference = pending.pop()
        if not isinstance(reference, str) or not reference.startswith("#/$defs/") or "%" in reference:
            return schema
        parts = [part.replace("~1", "/").replace("~0", "~") for part in reference[8:].split("/")]
        name = parts[0]
        target: Any = definitions
        for part in parts:
            if not isinstance(target, dict) or part not in target:
                return schema
            target = target[part]
        if name not in retained:
            retained.add(name)
            pending.extend(node["$ref"] for node in _schema_nodes(definitions[name]) if "$ref" in node)
    result = {key: value for key, value in schema.items() if key != "$defs"}
    if retained:
        result["$defs"] = {name: value for name, value in definitions.items() if name in retained}
    return result


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
    if _scoped_references(schema):
        return compact_input_contract(schema)
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
            # Sibling constraints have their own evaluation scope. Even disjoint
            # keys cannot be merged safely (e.g. additionalProperties inside the
            # target and properties beside the reference).
            conflicts = bool(node.keys() - {"$ref", "title", "description", "default", "examples", "$comment", "$defs", "definitions"})
            if isinstance(target, dict) and not conflicts:
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

    return _prune_unused_definitions(compact_input_contract(visit(schema)))


def render_tool_definition(payload: dict[str, Any], *, view: str = "input") -> str:
    excluded = (set() if view == "full" else
                {"input_schema", "example"} if view == "output" else
                {"output_schema", "example"})
    return render_structured_for_llm({
        key: compact_input_contract(value) if key in {"input_schema", "output_schema"} else value
        for key, value in payload.items()
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
    suggestions = []
    for suggestion in payload.get('suggestions', ()):
        projected = {key: value for key, value in suggestion.items() if key not in {'score', 'retrieval_sources'}}
        if suggestion.get('contract') is not None:
            contract = {key: value for key, value in suggestion['contract'].items()
                        if key not in {'score', 'search_text', 'input_shape', 'search_auxiliary_terms'}}
            for key in ('alias', 'purpose', 'matched_terms', 'unmatched_terms'):
                if key in contract and key in projected and projected[key] == contract[key]:
                    del projected[key]
            projected['contract'] = contract
        suggestions.append(projected)
    result = {**payload, 'hits': hits}
    if 'suggestions' in payload:
        result['suggestions'] = suggestions
    return render_structured_for_llm(result)
