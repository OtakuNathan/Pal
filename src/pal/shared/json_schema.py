"""Traversal of JSON Schema objects shared by tool projection and wire encoding."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_schema_nodes(schema: Any) -> Iterator[dict[str, Any]]:
    """Walk schema positions, never enum/default/const/example data."""
    if not isinstance(schema, dict):
        return
    yield schema
    for key, value in schema.items():
        if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"} and isinstance(value, dict):
            for child in value.values():
                yield from iter_schema_nodes(child)
        elif key in {"items", "additionalItems", "additionalProperties", "unevaluatedProperties",
                     "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else", "contentSchema"}:
            for child in value if isinstance(value, list) else [value]:
                yield from iter_schema_nodes(child)
        elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(value, list):
            for child in value:
                yield from iter_schema_nodes(child)
