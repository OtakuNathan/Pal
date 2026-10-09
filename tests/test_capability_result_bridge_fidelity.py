"""Capability-call relays must keep typed effects and contextual evidence."""

import asyncio

import pytest

from pal.execution.contracts import CapabilityCall
from pal.execution.tool_facade import (
    CompleteResult, EmptyToolInput, EmptyToolOutput, EffectOutcome,
    FailedResult, RetryDirective,
)
from pal.shared.tool_protocol import ToolAffordance, ToolContextMessageIR
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import runtime


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_capability_call_bridge_keeps_effect_guidance_and_context(runtime, asynchronous, failed):
    context = (ToolContextMessageIR(content="Retained reference evidence", semantic_kind="reference"),)
    ref = runtime.result_snapshots.capture("Retained original output", call_id="bridge", lifetime="review")
    action = ToolAffordance(tool="read_tool", arguments={"name": "bridge_target"}, reason="Inspect contract")
    common = dict(llm_text="result evidence", context_messages=context, snapshot_refs=(ref,),
        affordances=[action], recovery_hint="Inspect current state before any further changes.")
    raw = (FailedResult(error_code="partial_failure", error="operation partially applied",
        effect=EffectOutcome.APPLIED, retry=RetryDirective.DO_NOT_RETRY, **common) if failed else
        CompleteResult(output={}, effect=EffectOutcome.NONE, context_delivery={"reference": "context"}, **common))
    mount_test_capability(runtime, alias="bridge_target", canonical_path="op_test_bridge_target",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: raw)
    call = CapabilityCall(name="call_tool", args={"name": "bridge_target", "args": {}})
    result = asyncio.run(runtime.execute_async(call)) if asynchronous else runtime.execute(call)
    assert result.effect_receipt is not None
    assert result.effect_receipt.outcome is raw.effect
    assert result.context_messages == context
    assert result.snapshot_refs == (ref,)
    assert result.affordances == (action,)
    assert result.recovery_hint == raw.recovery_hint
    if failed:
        assert result.structured["retry"] == "do_not_retry"
    else:
        assert result.context_delivery == raw.context_delivery
