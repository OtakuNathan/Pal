import copy
import json

import pytest
from jsonschema import Draft202012Validator

from pal.execution.tool_presentation import native_input_contract, render_tool_search


def test_native_projection_preserves_business_data_and_validation():
    schema = {
        "title": "Root", "type": "object", "required": ["title"],
        "properties": {"title": {"$ref": "#/$defs/Value"}},
        "$defs": {
            "Value": {"title": "Value", "type": "object", "required": ["title"],
                      "properties": {"title": {"type": "string", "enum": ["yes"]}},
                      "default": {"title": "yes"}, "const": {"title": "yes"}},
            "Unused": {"type": "string"},
        },
    }
    original = copy.deepcopy(schema)
    projected = native_input_contract(schema)
    assert schema == original
    assert "title" not in projected and "$defs" not in projected
    assert projected["properties"]["title"]["default"] == {"title": "yes"}
    assert projected["properties"]["title"]["const"] == {"title": "yes"}
    for value in ({}, {"title": {"title": "yes"}}, {"title": {"title": "no"}}, {"title": "yes"}):
        assert Draft202012Validator(projected).is_valid(value) == Draft202012Validator(schema).is_valid(value)


def test_recursive_and_transitive_definitions_remain_reachable():
    schema = {"$ref": "#/$defs/Node", "$defs": {
        "Node": {"type": "object", "properties": {
            "next": {"$ref": "#/$defs/Node"}, "value": {"$ref": "#/$defs/Value"}}},
        "Value": {"type": "integer", "minimum": 1}, "Unused": {"type": "string"}}}
    projected = native_input_contract(schema)
    assert set(projected["$defs"]) == {"Node", "Value"}
    for value in ({}, {"next": {"value": 3}}, {"next": {"value": 0}}):
        assert Draft202012Validator(projected).is_valid(value) == Draft202012Validator(schema).is_valid(value)


@pytest.mark.parametrize("extra", [
    {"$ref": "external.json#/$defs/Item"}, {"$ref": "#missing"},
    {"$dynamicRef": "#node"}, {"$id": "https://example.test/schema"},
    {"$ref": "#/$defs/Missing"},
])
def test_uncertain_reference_scope_keeps_definitions(extra):
    schema = {**extra, "$defs": {"Item": {"type": "string"}}}
    assert native_input_contract(schema)["$defs"] == schema["$defs"]


def test_ref_siblings_cannot_override_constraints():
    schema = {"type": "object", "properties": {
        "n": {"$ref": "#/$defs/N", "minimum": 1}},
        "$defs": {"N": {"type": "integer", "minimum": 5}, "Unused": {"type": "null"}}}
    projected = native_input_contract(schema)
    assert set(projected["$defs"]) == {"N"}
    for value in ({"n": 2}, {"n": 5}):
        assert Draft202012Validator(projected).is_valid(value) == Draft202012Validator(schema).is_valid(value)


def test_ref_siblings_keep_separate_object_evaluation_scope():
    schema = {"$ref": "#/$defs/Closed", "properties": {"extra": {"type": "string"}},
              "$defs": {"Closed": {"type": "object", "additionalProperties": False}}}
    projected = native_input_contract(schema)
    assert "$ref" in projected
    for value in ({}, {"extra": "must still be rejected"}):
        assert Draft202012Validator(projected).is_valid(value) == Draft202012Validator(schema).is_valid(value)


def test_suggestions_remove_only_equal_fields_without_mutating_saved_payload():
    payload = {"suggestions": [{
        "alias": "read_item", "purpose": "Read item", "matched_terms": ["read"],
        "unmatched_terms": ["encrypted"],
        "contract": {"alias": "read_item", "purpose": "Different purpose", "matched_terms": ["read"],
                     "unmatched_terms": ["encrypted"], "input_contract": {"type": "object"}},
    }]}
    saved = json.dumps(payload)
    result = json.loads(render_tool_search(None, payload))["suggestions"][0]
    assert "alias" not in result and "matched_terms" not in result
    assert result["purpose"] == "Read item"
    assert result["contract"]["unmatched_terms"] == ["encrypted"]
    assert json.dumps(payload) == saved
