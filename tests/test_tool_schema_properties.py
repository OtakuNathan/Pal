from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import Field, ValidationError
from typing import Literal

hypothesis = pytest.importorskip("hypothesis")
hypothesis_jsonschema = pytest.importorskip("hypothesis_jsonschema")
from hypothesis import given, settings, strategies as st
from hypothesis_jsonschema import from_schema

from pal.execution import generated_tool_models
from pal.execution.tool_facade import StrictToolModel
from pal.minion.scoped_execution import MinionScopedExecutionOpMinionArtifactEditInput
from pal.minion.v2.capabilities import (
    MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput,
    MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput,
)
from pal.minion.v2.review_findings import MinionV2ReviewAddFindingInput


class PropertyInput(StrictToolModel):
    name: str = Field(min_length=1, max_length=20)
    count: int = Field(ge=1, le=5)
    mode: Literal["one", "two"]
    tags: list[str] | None = Field(default=None, max_length=3)


MODEL = PropertyInput
GENERATED_SCHEMA = MODEL.model_json_schema(mode="validation")
DECLARED_INPUT_MODELS = tuple(sorted({
    name: value
    for name, value in (
        *(
            (name, value)
            for name, value in vars(generated_tool_models).items()
            if name.endswith("Input")
            and isinstance(value, type)
            and issubclass(value, StrictToolModel)
        ),
        ("MinionV2ReviewAddFindingInput", MinionV2ReviewAddFindingInput),
        (
            "MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput",
            MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput,
        ),
        (
            "MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput",
            MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput,
        ),
        (
            "MinionScopedExecutionOpMinionArtifactEditInput",
            MinionScopedExecutionOpMinionArtifactEditInput,
        ),
    )
}.items()))


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


@pytest.mark.parametrize("_name,model", DECLARED_INPUT_MODELS, ids=[name for name, _model in DECLARED_INPUT_MODELS])
@settings(max_examples=3, deadline=None)
@given(data=st.data())
def test_every_declared_input_model_matches_its_draft_schema_for_generated_values(_name, model, data) -> None:
    schema = model.model_json_schema(mode="validation")
    value = data.draw(from_schema(schema))
    pydantic_value = model.model_validate(value, strict=True).model_dump(mode="json", exclude_none=True)
    Draft202012Validator(schema).validate(value)
    assert isinstance(pydantic_value, dict)


@pytest.mark.parametrize("_name,model", DECLARED_INPUT_MODELS, ids=[name for name, _model in DECLARED_INPUT_MODELS])
def test_every_declared_input_model_and_schema_reject_extra_fields(_name, model) -> None:
    schema = model.model_json_schema(mode="validation")
    invalid = {"__unexpected_tool_argument__": True}
    with pytest.raises(ValidationError):
        model.model_validate(invalid, strict=True)
    assert list(Draft202012Validator(schema).iter_errors(invalid))
