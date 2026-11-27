"""Ordinary completion is terse; effect evidence stays typed and errors stay explicit."""
from dataclasses import replace

import pytest

from pal.execution.runtime import ExecutionRuntime
from pal.execution.result_snapshots import render_snapshot_hint, snapshot_hint_variants
from pal.execution.shell_exec import SHELL_EXEC_GUIDANCE
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, EffectReceipt, ToolHandlerResult
from pal.execution.tool_semantics import DIRECT_NONE, DIRECT_LOCAL_READ, INDIRECT_LOCAL_READ, INDIRECT_NONE
from pal.llm.ir import LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR, GenerationPolicyIR, WireShape
from pal.llm.serde import message_to_payload, message_from_payload
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.result_snapshot import ResultSnapshotRef
from pal.shared.tool_protocol import CompleteResult, EffectOutcome, FailedResult, RejectedResult, RetryDirective, ToolAffordance, ToolCallIR, ToolResultIR
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import invoke, runtime


@pytest.mark.parametrize("semantics,effect", [
    (DIRECT_NONE, EffectOutcome.NONE),
    (DIRECT_LOCAL_READ, EffectOutcome.APPLIED),
    (INDIRECT_LOCAL_READ, EffectOutcome.APPLIED),
])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_routine_success_only_delivers_exact_body(runtime, semantics, effect, asynchronous):
    body = "  result\n "
    mount_test_capability(runtime, alias="routine", canonical_path="op_test_routine",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, execution=semantics,
        handler=lambda _: ToolHandlerResult(output={}, llm_text=body, effect_receipt=EffectReceipt(outcome=effect)))
    result = invoke(runtime, "routine", {}, asynchronous=asynchronous)
    assert result.ok and result.llm_text == body
    assert result.invocation_result.effect == effect


@pytest.mark.parametrize("semantics", [DIRECT_NONE, INDIRECT_NONE])
def test_discrepant_actual_effect_survives_wrappers(runtime, semantics):
    mount_test_capability(runtime, alias="discrepant", canonical_path="op_test_discrepant",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, execution=semantics,
        handler=lambda _: ToolHandlerResult(output={}, llm_text="body",
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED)))
    result = invoke(runtime, "discrepant", {})
    assert result.llm_text == 'body\n\nTool result metadata: {"effect":"applied"}'


def test_success_metadata_only_keeps_dynamic_guidance_and_exception_facts():
    body = CompleteResult(output={}, llm_text="done", effect=EffectOutcome.APPLIED)
    action = ToolAffordance(tool="read_tool", arguments={"name": "example"}, reason="Inspect this result.")
    render = ExecutionRuntime._invocation_metadata_values
    assert render(body) == {}
    assert render(body.model_copy(update={"affordances": [action]})) == {"affordances": [action.model_dump(mode="json")]}
    assert render(body.model_copy(update={"effect": EffectOutcome.NONE, "recovery_hint": "Repair this item."})) == {"recovery": "Repair this item."}
    assert render(body.model_copy(update={"output_error": "Snapshot failed."})) == {"effect": "applied", "delivery_error": "Snapshot failed."}
    assert render(body.model_copy(update={"effect": EffectOutcome.UNKNOWN})) == {"effect": "unknown"}
    assert render(body.model_copy(update={"recovery_hint": "Only retry failed items."})) == {
        "effect": "applied", "recovery": "Only retry failed items."}
    rejected = RejectedResult(error_code="refused", error="bad input", llm_text="bad input", retry=RetryDirective.CORRECT_INPUT)
    assert render(rejected) == {"kind": "rejected", "effect": "not_started", "error_code": "refused", "retry": "correct_input"}
    failed = FailedResult(error_code="validation", error="failed", llm_text="failed", effect=EffectOutcome.APPLIED,
        retry=RetryDirective.DO_NOT_RETRY, operation_result="original", effect_receipt={"updated": True})
    assert render(failed) == {"kind": "failed", "effect": "applied", "error_code": "validation", "retry": "do_not_retry",
                              "operation_result": "original", "effect_receipt": {"updated": True}}


@pytest.mark.parametrize("shape", list(WireShape))
def test_local_effect_roundtrips_but_provider_payload_does_not_change(shape):
    result = LLMMessageIR(MessageRole.TOOL, (ToolResultIR("c", "example", "done"),))
    changed = replace(result, parts=(replace(result.parts[0], effect=EffectOutcome.APPLIED),))
    legacy = message_to_payload(result)
    assert "effect" not in legacy["parts"][0]
    assert message_from_payload(legacy) == result
    saved = message_to_payload(changed)
    assert saved["parts"][0]["effect"] == "applied"
    assert message_from_payload(saved) == changed
    req = LLMRequestIR((
        LLMMessageIR(MessageRole.USER, (TextPartIR("task"),)),
        LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("c", "example", {}),)),
        result,
    ), (), GenerationPolicyIR(max_output_tokens=128))
    ctx = ShapeContext(shape, "fixture", "fixture")
    codec = codec_for_shape(shape)
    assert codec.encode(req, ctx).payload == codec.encode(replace(req, messages=(*req.messages[:-1], changed)), ctx).payload


def test_snapshot_hint_has_only_dynamic_facts_and_maintains_legacy_compatibility():
    ref = ResultSnapshotRef("fixture", "/example/output/abc123.txt", "digest", 100, coverage="complete result text")
    short = render_snapshot_hint(ref)
    assert short == "Output snapshot: /example/output/abc123.txt\nCoverage: complete result text."
    old, new = snapshot_hint_variants(ref)
    assert new == short and old.startswith(short)
    for phrase in ["immutable captured results", "rg -n", "wc -lc", "bounded ranges", "never repeat a side effect"]:
        assert phrase in SHELL_EXEC_GUIDANCE.use_when


def test_transcript_adapter_preserves_local_effect():
    from pal.memory.service import _transcript_from_turn, _turn_from_transcript
    from pal.memory.turn_ir import L1TurnIR, L1TurnState

    messages = (
        LLMMessageIR(MessageRole.USER, (TextPartIR("task"),)),
        LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("c", "example", {}),)),
        LLMMessageIR(MessageRole.TOOL, (ToolResultIR("c", "example", "done", effect=EffectOutcome.APPLIED),)),
        LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("complete"),)),
    )
    turn = L1TurnIR("fixture", messages, state=L1TurnState.SETTLED)
    transcript = _transcript_from_turn(turn)
    assert transcript[2].payload["_pal_result_state"]["effect"] == "applied"
    recovered = _turn_from_transcript("fixture", transcript)
    restored = next(p for m in recovered.messages for p in m.parts if isinstance(p, ToolResultIR))
    assert restored.content == "done" and restored.effect == EffectOutcome.APPLIED


def test_core_commits_terse_body_with_local_effect():
    import asyncio
    from types import SimpleNamespace

    from pal.memory import MemoryService, register_with_core as register_memory
    from pal.shared.tool_protocol import ToolExecutionResult
    from tests.core_factory import build_core

    core = build_core()
    memory = MemoryService()
    register_memory(core.context, memory)
    call = ToolCallIR("c", "example", {})
    memory.begin_l1_turn("T", user_text="task")
    memory.upsert_l1_assistant("T", LLMMessageIR(MessageRole.ASSISTANT, (call,)))
    result = ToolExecutionResult(name=call.name, ok=True, call_id=call.call_id, llm_text="done",
        invocation_result=CompleteResult(output={}, llm_text="done", effect=EffectOutcome.APPLIED))
    try:
        asyncio.run(core.turn_executor._append_l1_tool_result_async(SimpleNamespace(turn_id="T"), call, result))
        committed = memory.active_l1_turn("T").messages[-1].parts[0]
        assert isinstance(committed, ToolResultIR)
        assert committed.content == "done" and committed.effect == EffectOutcome.APPLIED
    finally:
        core.close()
