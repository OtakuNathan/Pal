from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

hypothesis = pytest.importorskip("hypothesis")
hypothesis_jsonschema = pytest.importorskip("hypothesis_jsonschema")
from hypothesis import given
from hypothesis_jsonschema import from_schema

from pal.execution.tool_registry import model_from_json_schema


SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 20},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "mode": {"type": "string", "enum": ["one", "two"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["name", "count", "mode"],
    "additionalProperties": False,
}
MODEL = model_from_json_schema("PropertyInput", SCHEMA, input_contract=True)
GENERATED_SCHEMA = MODEL.model_json_schema(mode="validation")


@given(from_schema(GENERATED_SCHEMA))
def test_pydantic_and_draft_2020_12_accept_the_same_generated_valid_values(value) -> None:
    MODEL.model_validate(value, strict=True)
    Draft202012Validator(GENERATED_SCHEMA).validate(value)


@pytest.mark.parametrize(
    "value",
    [
        {"count": 1, "mode": "one"},
        {"name": "x", "count": 1, "mode": "one", "extra": True},
        {"name": "x", "count": "1", "mode": "one"},
        {"name": "x", "count": 1, "mode": "three"},
        {"name": "x", "count": 9, "mode": "one"},
        {"name": "x", "count": 1, "mode": "one", "tags": ["a", "b", "c", "d"]},
    ],
)
def test_pydantic_and_draft_2020_12_reject_the_same_invalid_values(value) -> None:
    with pytest.raises(ValidationError):
        MODEL.model_validate(value, strict=True)
    errors = list(Draft202012Validator(GENERATED_SCHEMA).iter_errors(value))
    assert errors
