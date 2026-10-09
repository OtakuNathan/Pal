"""Validated nullable output fields must survive delivery to the model."""

from dataclasses import replace
import json

import pytest
from jsonschema import Draft202012Validator

from pal.execution.tool_facade import (
    EmptyToolInput, StrictToolModel, ToolHandlerResult, EffectReceipt, EffectOutcome,
    dump_output,
)
from pal.execution.tool_semantics import DIRECT_LOCAL_READ, INDIRECT_LOCAL_READ
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import runtime, invoke


class _NullableState(StrictToolModel):
    active_provider: str | None
    enabled: bool
    count: int
    optional_note: str | None = None


class _NullableOutput(StrictToolModel):
    state: _NullableState
    states: list[_NullableState]


PAYLOAD = {
    "state": {"active_provider": None, "enabled": False, "count": 0},
    "states": [{"active_provider": None, "enabled": False, "count": 0}],
}


def test_explicit_optional_null_is_distinct_from_omission():
    omitted = _NullableState.model_validate(PAYLOAD["state"])
    explicit = _NullableState.model_validate({**PAYLOAD["state"], "optional_note": None})
    assert "optional_note" not in dump_output(omitted)
    assert dump_output(explicit)["optional_note"] is None


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_nullable_output_survives_runtime_delivery(runtime, direct, asynchronous):
    mount_test_capability(
        runtime, alias="nullable_state", canonical_path="op_test_nullable_state",
        InputModel=EmptyToolInput, OutputModel=_NullableOutput,
        execution=DIRECT_LOCAL_READ if direct else INDIRECT_LOCAL_READ,
        handler=lambda _: ToolHandlerResult(output=PAYLOAD, effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE)),
    )
    result = invoke(runtime, "nullable_state", {}, asynchronous=asynchronous)
    assert result.ok, result.llm_text
    assert result.structured == PAYLOAD
    assert json.loads(result.llm_text.split("\n\nTool result metadata:", 1)[0]) == PAYLOAD
    Draft202012Validator(_NullableOutput.model_json_schema()).validate(result.structured)


def test_builtin_nullable_output_survives_validation(runtime):
    from pal.execution.runtime import ExecutionRuntime

    record = replace(runtime.registry_generation.record_for_alias("read_tool"),
        output_model=_NullableOutput, output_schema=_NullableOutput.model_json_schema())
    result = ExecutionRuntime._complete_builtin(record, PAYLOAD)
    assert result.kind == "complete"
    assert result.output == PAYLOAD
    assert json.loads(result.llm_text) == PAYLOAD
    Draft202012Validator(record.output_schema).validate(result.output)
