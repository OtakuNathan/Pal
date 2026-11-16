"""Draft-07 tool schemas are translated into Pal's 2020-12 dialect at ingestion.

Registration, discovery vocabulary and execution speak Draft 2020-12 only; the
original payload stays in ``raw`` as protocol evidence, and unknown dialects
remain quarantined rather than reinterpreted.
"""
from __future__ import annotations

import pytest
from jsonschema import Draft7Validator, Draft202012Validator

from pal.mcp.model import McpProtocolError
from pal.mcp.normalize import normalize_tool_payload, translate_tool_schema
from pal.mcp.protocol import validate_discovery, validate_tool_schema

# Exact schema observed from the quarantined zai-vision server (2026-10-29).
ZAI_UI_TO_ARTIFACT = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "additionalProperties": False,
    "properties": {
        "image_source": {
            "description": "Local file path or remote URL to the image",
            "type": "string",
        },
        "output_type": {
            "description": "Type of output to generate. Options: 'code', 'prompt', 'spec', 'description'.",
            "enum": ["code", "prompt", "spec", "description"],
            "type": "string",
        },
        "prompt": {
            "description": "Detailed instructions describing what to generate from this UI image.",
            "type": "string",
        },
    },
    "required": ["image_source", "output_type", "prompt"],
    "type": "object",
}


def test_draft_07_declaration_is_translated_and_registers() -> None:
    spec = normalize_tool_payload({"name": "ui_to_artifact", "inputSchema": ZAI_UI_TO_ARTIFACT})
    assert spec.input_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert spec.raw["inputSchema"] == ZAI_UI_TO_ARTIFACT
    validate_discovery((spec,), ())
    validate_tool_schema(spec.input_schema, "ui_to_artifact.inputSchema")


def test_draft_07_definitions_refs_and_tuple_items_are_canonicalized() -> None:
    draft7 = {
        "$schema": "https://json-schema.org/draft-07/schema#",
        "type": "object",
        "definitions": {
            "pair": {
                "type": "array",
                "items": [{"type": "string"}, {"type": "integer"}],
                "additionalItems": False,
            }
        },
        "properties": {"pairs": {"type": "array", "items": {"$ref": "#/definitions/pair"}}},
        "additionalProperties": False,
    }
    spec = normalize_tool_payload({"name": "pairs", "inputSchema": draft7})
    assert spec.input_schema["$defs"]["pair"]["prefixItems"] == [{"type": "string"}, {"type": "integer"}]
    assert spec.input_schema["$defs"]["pair"]["items"] is False
    assert spec.input_schema["properties"]["pairs"]["items"]["$ref"] == "#/$defs/pair"
    assert "definitions" not in spec.input_schema
    validate_discovery((spec,), ())


@pytest.mark.parametrize("dialect", [
    "http://json-schema.org/draft-04/schema#",
    "https://json-schema.org/draft-06/schema#",
])
def test_unknown_dialects_are_not_translated_and_stay_rejected(dialect: str) -> None:
    schema = {"$schema": dialect, "type": "object", "properties": {}, "additionalProperties": False}
    assert translate_tool_schema(schema) is schema
    spec = normalize_tool_payload({"name": "odd", "inputSchema": schema})
    with pytest.raises(McpProtocolError):
        validate_discovery((spec,), ())


def test_pal_dialect_schemas_pass_through_unchanged() -> None:
    native = {"type": "object", "properties": {}, "additionalProperties": False}
    assert translate_tool_schema(native) is native
    assert translate_tool_schema(None) is None
    translated = translate_tool_schema(ZAI_UI_TO_ARTIFACT)
    assert translate_tool_schema(translated) == translated


@pytest.mark.parametrize("keywords, instances", [
    ({"dependencies": {"name": ["value"]}}, [{}, {"name": "x"}, {"name": "x", "value": 1}]),
    ({"dependencies": {"name": {"required": ["value"]}}}, [{}, {"name": "x"}, {"name": "x", "value": 1}]),
    ({"properties": {"value": {"type": "array", "items": {"type": "integer"}, "additionalItems": False}}},
     [{"value": []}, {"value": [1, 2]}, {"value": ["bad"]}]),
    ({"properties": {"value": {"type": "array", "additionalItems": False}}},
     [{"value": []}, {"value": [1, 2]}]),
    ({"properties": {"value": {"type": "array", "additionalItems": False,
                               "items": [{"type": "integer"}]}}},
     [{"value": []}, {"value": [1]}, {"value": [1, 2]}, {"value": ["bad"]}]),
    ({"definitions": {"number": {"type": "integer"}},
      "properties": {"value": {"$ref": "#/definitions/number", "type": "string", "minimum": 100}}},
     [{"value": 1}, {"value": "bad"}]),
    ({"definitions": {"tuple": {"items": [{"type": "integer"}]}},
      "properties": {"value": {"$ref": "#/definitions/tuple/items/0"}}},
     [{"value": 1}, {"value": "bad"}]),
    ({"dependencies": {"name": {"properties": {"value": {"type": "integer"}}}},
      "properties": {"value": {"$ref": "#/dependencies/name/properties/value"}}},
     [{}, {"value": 1}, {"value": "bad"}]),
    ({"definitions": {"a/b": {"type": "integer"}},
      "properties": {"value": {"$ref": "#/definitions/a~1b"}}},
     [{"value": 1}, {"value": "bad"}]),
])
def test_translation_preserves_validation_results(keywords, instances):
    source = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", **keywords}
    translated = translate_tool_schema(source)
    validate_tool_schema(translated, "translated")
    for instance in instances:
        assert Draft7Validator(source).is_valid(instance) == Draft202012Validator(translated).is_valid(instance)


def test_annotation_and_literal_data_are_not_translated():
    data = {"$schema": "arbitrary user data", "definitions": {"item": 1}, "items": [1, 2],
            "additionalItems": False, "$ref": "#/definitions/item"}
    source = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
              "default": data, "examples": [data], "const": data, "enum": [data],
              "properties": {"definitions": {"type": "object"}, "items": {"type": "array"}}}
    translated = translate_tool_schema(source)
    for key in ("default", "examples", "const", "enum"):
        assert translated[key] == source[key]
    assert translated["properties"] == source["properties"]
    assert Draft202012Validator(translated).is_valid(data)


@pytest.mark.parametrize("keywords", [
    {"prefixItems": [{"type": "integer"}]},
    {"unevaluatedProperties": False},
    {"definitions": {"mixed": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}},
    {"properties": {"broken": {"type": 42}}},
    {"properties": {"remote": {"$ref": "https://example.test/schema"}}},
])
def test_ambiguous_or_invalid_draft07_is_rejected(keywords):
    with pytest.raises(McpProtocolError):
        translate_tool_schema({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", **keywords})
