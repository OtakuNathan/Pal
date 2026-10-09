"""Strict, offline MCP 2025-06-18 boundary validation.

Schema vendored from the modelcontextprotocol/2025-06-18 tag, under the
adjacent SCHEMA_LICENSE.txt. No schema or reference is fetched at runtime.
"""
from __future__ import annotations

import base64
import binascii
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft7Validator, Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from pal.mcp.model import McpProtocolError

PROTOCOL_VERSION = "2025-06-18"
_SCHEMA = json.loads(files("pal.mcp").joinpath("schema_2025_06_18.json").read_text())
_FORMATS = FormatChecker()


@_FORMATS.checks("byte", raises=(ValueError, binascii.Error))
def _base64(value):
    if isinstance(value, str):
        base64.b64decode(value, validate=True)
    return True


@lru_cache(maxsize=None)
def _validator(definition: str):
    return Draft7Validator({"$ref": f"#/definitions/{definition}",
                            "definitions": _SCHEMA["definitions"]}, format_checker=_FORMATS)


def validate_message(value: Any, definition: str) -> None:
    error = next(_validator(definition).iter_errors(value), None)
    if error is not None:
        path = "/".join(map(str, error.absolute_path)) or "<root>"
        raise McpProtocolError(f"Invalid MCP {definition} at {path} ({error.validator})",
                               payload={"raw_response": value, "validation_error": error.message})


def validate_tool_schema(schema: Any, label: str) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise McpProtocolError(f"{label} must explicitly declare type=object", payload={"schema": schema})
    # Execution uses Draft 2020-12. Unsupported dialects and remote references
    # are rejected at attachment, never fetched or silently reinterpreted.
    if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") not in {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }:
        raise McpProtocolError(f"Unsupported JSON Schema dialect in {label}", payload={"schema": schema})
    try:
        Draft202012Validator.check_schema(schema)
        visited: set[int] = set()
        def walk(node):
            if not isinstance(node, dict) or id(node) in visited:
                return
            visited.add(id(node))
            if "$id" in node and node is not schema:
                raise ValueError("Nested schema resource identities are not supported")
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"}:
                    if key == "$dynamicRef" or not isinstance(value, str) or not value.startswith("#/"):
                        raise ValueError("Only local JSON Pointer $ref references are supported")
                    target = schema
                    for part in value[2:].split("/"):
                        part = part.replace("~1", "/").replace("~0", "~")
                        target = target[int(part)] if isinstance(target, list) else target[part]
                    if not isinstance(target, (dict, bool)):
                        raise ValueError("Schema reference does not target a schema")
                    walk(target)
                elif key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}:
                    for child in value.values():
                        walk(child)
                elif key in {"allOf", "anyOf", "oneOf", "prefixItems"}:
                    for child in value:
                        walk(child)
                elif key in {"items", "contains", "additionalProperties", "unevaluatedProperties",
                             "unevaluatedItems", "propertyNames", "not", "if", "then", "else"}:
                    walk(value)
        walk(schema)
    except (SchemaError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise McpProtocolError(f"Invalid or unsupported {label}", payload={"schema": schema}) from exc


def validate_discovery(tools, prompts) -> None:
    for items, definition in ((tools, "Tool"), (prompts, "Prompt")):
        names = set()
        for item in items:
            if not item.name or item.name in names:
                raise McpProtocolError(f"Empty or duplicate {definition} name: {item.name!r}")
            names.add(item.name)
            if definition == "Tool":
                payload = {"name": item.name, "inputSchema": item.input_schema, "annotations": item.annotations}
                if item.output_schema is not None:
                    payload["outputSchema"] = item.output_schema
                    validate_tool_schema(item.output_schema, f"{item.name}.outputSchema")
                validate_message(payload, definition)
                validate_tool_schema(item.input_schema, f"{item.name}.inputSchema")
            else:
                validate_message({"name": item.name, "arguments": [
                    {"name": arg.name, "description": arg.description, "required": arg.required}
                    for arg in item.arguments]}, definition)
                args = [arg.name for arg in item.arguments]
                if any(not name for name in args) or len(set(args)) != len(args):
                    raise McpProtocolError(f"Empty or duplicate prompt argument: {item.name}")


def validate_tool_result(result: Any, output_schema=None) -> None:
    validate_message(result, "CallToolResult")
    if output_schema is not None and not result.get("isError", False):
        value = result.get("structuredContent")
        error = next(Draft202012Validator(output_schema).iter_errors(value), None)
        if error is not None:
            raise McpProtocolError("MCP structuredContent violates outputSchema", payload={
                "raw_response": result, "validation_error": error.message})
