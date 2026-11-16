"""Translate declared Draft-07 tool schemas without rewriting instance data."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from pal.mcp.model import McpProtocolError


_DRAFT_07_DIALECTS = frozenset(
    f"{scheme}://json-schema.org/draft-07/schema{fragment}"
    for scheme in ("http", "https") for fragment in ("", "#")
)
_DIALECT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_MAPS = {"properties", "patternProperties", "definitions"}
_SCHEMA_ARRAYS = {"allOf", "anyOf", "oneOf"}
_SCHEMAS = {"additionalProperties", "contains", "propertyNames", "not", "if", "then", "else"}
_NEW_KEYWORDS = {
    "$defs", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveRef", "$recursiveAnchor",
    "$vocabulary", "prefixItems", "dependentRequired", "dependentSchemas",
    "unevaluatedProperties", "unevaluatedItems", "minContains", "maxContains", "contentSchema",
}


def _pointer(path: tuple[str, ...]) -> str:
    return "#/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def translate_tool_schema(schema: Any) -> Any:
    """Translate supported Draft-07 semantics; reject ambiguous mixed dialects.

    Only schema positions are traversed. Defaults, examples, const/enum values
    and extension data remain byte-for-byte equivalent JSON values. Local refs
    follow moved definitions, tuple items and schema dependencies.
    """
    if not isinstance(schema, dict) or schema.get("$schema") not in _DRAFT_07_DIALECTS:
        return schema
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise McpProtocolError("Invalid Draft-07 tool schema", payload={"schema": schema}) from exc

    locations: dict[str, str] = {}
    references: list[dict] = []

    def translate(node: Any, source: tuple[str, ...], target: tuple[str, ...]) -> Any:
        locations[_pointer(source)] = _pointer(target)
        if isinstance(node, bool):
            return node
        if node.get("$schema", schema["$schema"]) not in _DRAFT_07_DIALECTS or _NEW_KEYWORDS & node.keys():
            raise McpProtocolError("Mixed JSON Schema dialects are not supported", payload={"schema": schema})
        result = {}
        for key, value in node.items():
            # Draft-07 ignores siblings of $ref. Keep annotations and schema
            # locations for reference resolution, but never activate assertions.
            if "$ref" in node and key not in {
                "$ref", "$schema", "definitions", "title", "description", "default", "examples", "$comment",
            }:
                continue
            if key == "$schema":
                result[key] = _DIALECT_2020_12
            elif key == "$ref":
                result[key] = value
                references.append(result)
            elif key in _SCHEMA_MAPS:
                destination = "$defs" if key == "definitions" else key
                result[destination] = {
                    name: translate(child, (*source, key, name), (*target, destination, name))
                    for name, child in value.items()
                }
            elif key in _SCHEMA_ARRAYS or (key == "items" and isinstance(value, list)):
                destination = "prefixItems" if key == "items" else key
                result[destination] = [
                    translate(child, (*source, key, str(index)), (*target, destination, str(index)))
                    for index, child in enumerate(value)
                ]
            elif key in _SCHEMAS or key == "items":
                result[key] = translate(value, (*source, key), (*target, key))
            elif key == "additionalItems":
                if isinstance(node.get("items"), list):
                    result["items"] = translate(value, (*source, key), (*target, "items"))
            elif key == "dependencies":
                for name, dependency in value.items():
                    if isinstance(dependency, list):
                        result.setdefault("dependentRequired", {})[name] = list(dependency)
                    else:
                        result.setdefault("dependentSchemas", {})[name] = translate(
                            dependency, (*source, key, name), (*target, "dependentSchemas", name))
            else:
                result[key] = deepcopy(value)
        return result

    translated = translate(schema, (), ())
    for node in references:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or unquote(ref) not in locations:
            raise McpProtocolError("Draft-07 refs must target supported local schema positions",
                                   payload={"schema": schema})
        node["$ref"] = locations[unquote(ref)]
    return translated
