"""Lossless continuation and request-admission regressions. No provider calls."""
import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from pal.llm.ir import LLMMessageIR, MessageRole, ReasoningPartIR, TextPartIR, WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, _JSONFrame
from pal.llm.transport import _json_mapping
from pal.llm.serde import message_from_payload, message_to_payload
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR
from tests.test_v3_n1_root_lifecycle import user
from tests.test_v3_n3_vertical_trace import _runtime, _executor, _request_for, _drive


ARGUMENTS = '{  "z": 1, "a": "\\u4e2d" }'


def native_message(index=0):
    return {"role": "assistant", "content": None,
            "reasoning_content": f"REASON-{index}", "vendor_signature": f"SIG-{index}",
            "tool_calls": [{"id": f"call-{index}", "type": "function", "function": {
                "name": "read_file", "arguments": ARGUMENTS}}]}


class NativeTransport:
    def __init__(self):
        self.captured = []

    def frames(self, endpoint, request):
        index = len(self.captured)
        self.captured.append(request)
        yield _JSONFrame(0, {"choices": [{"message": native_message(index), "finish_reason": "tool_calls"}]})


def test_sdk_mapping_preserves_explicit_null_and_absent_fields():
    class Reply(BaseModel):
        content: str | None = None
        absent: str | None = None
    assert _json_mapping(Reply(content=None)) == {"content": None}


def test_completion_roundtrip_preserves_protocol_values():
    shape = WireShape.OPENAI_COMPLETION
    context = ShapeContext(shape, "endpoint", "model")
    codec = codec_for_shape(shape)
    updates = list(codec.decode(iter([_JSONFrame(0, {
        "choices": [{"message": native_message(), "finish_reason": "tool_calls"}],
    })]), context))
    message = updates[-1].response.message
    assert thaw_json(message.replay.payload["message"]) == native_message()
    assert message_from_payload(message_to_payload(message)) == message


def test_completion_stream_keeps_argument_string_and_extension():
    shape = WireShape.OPENAI_COMPLETION
    ctx = ShapeContext(shape, "endpoint", "model")
    deltas = [
        {"role": "assistant", "content": None, "vendor_signature": "opaque"},
        {"reasoning_content": "step "}, {"reasoning_content": "one"},
        {"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {
            "name": "read_file", "arguments": ARGUMENTS[:12]}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": ARGUMENTS[12:]}}]},
    ]
    frames = [_JSONFrame(i, {"choices": [{"delta": d}]}) for i, d in enumerate(deltas)]
    frames.append(_JSONFrame(len(frames), {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
    message = list(codec_for_shape(shape).decode(iter(frames), ctx))[-1].response.message
    wire = message.replay.payload["message"]
    assert wire["content"] is None
    assert wire["reasoning_content"] == "step one"
    assert wire["vendor_signature"] == "opaque"
    assert wire["tool_calls"][0]["function"]["arguments"] == ARGUMENTS


def test_admission_freezes_only_the_captured_prefix():
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("question", "q"))
    root = memory.history_root
    prepared = root.prepare_submission(("q",))
    assert not root.left_messages()
    memory.upsert_l1_assistant("T", LLMMessageIR(
        MessageRole.ASSISTANT, (ReasoningPartIR("keep"), TextPartIR("new")), message_id="a"))
    assert root.commit_submission(prepared, ("q",))
    assert [m.message_id for m in root.left_messages()] == ["q"]
    assert [m.message_id for m in root.right_messages()] == ["a"]
    assert not root.commit_submission(prepared, ("q",))
    root.reset()
    assert not root.commit_submission(prepared, ("q",))


def test_wrong_effective_input_cannot_freeze_snapshot():
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("question", "q"))
    root = memory.history_root
    prepared = root.prepare_submission(("q",))
    assert not root.commit_submission(prepared, ())
    assert not root.left_messages()


@pytest.mark.parametrize("stream", [False, True])
def test_submission_is_delivered_on_owner_before_output(stream):
    owner = threading.get_ident()
    observed = []
    signal = threading.Event()
    class Transport:
        def frames(self, endpoint, request):
            request.on_submitted()
            assert signal.wait(3), "owner must observe admission while generation is still pending"
            yield _JSONFrame(0, {"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]})
    runtime = _runtime(Transport())
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("question", "q"))
    root = memory.history_root
    snapshot = root.prepare_submission(("q",))
    def admitted(receipt):
        observed.append(threading.get_ident())
        assert root.commit_submission(snapshot, receipt.message_ids)
        signal.set()
    async def run():
        request = _request_for(memory, ())
        if stream:
            async for _ in runtime.astream(request, on_submitted=admitted):
                pass
        else:
            await runtime.agenerate(request, on_submitted=admitted)
    asyncio.run(run())
    assert observed == [owner]
    assert [m.message_id for m in root.left_messages()] == ["q"]


def test_pre_send_failure_keeps_right():
    class Transport:
        def frames(self, endpoint, request):
            raise RuntimeError("not submitted")
            yield
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("q", "q"))
    runtime = _runtime(Transport())
    executor = _executor(memory, runtime)
    executor._handle_llm_provider_errors = False
    _drive(executor, _request_for(memory, ()))
    assert not memory.history_root.left_messages()


def test_100_tool_rounds_survive_settle_restore_and_projection_rebuild():
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("q", "q"))
    transport = NativeTransport()
    runtime = _runtime(transport)
    # Reproduce the old pre-hook capture / replay=None loss boundary.
    def normalize(**kwargs):
        for update in kwargs["updates"]:
            yield replace(update, response=replace(update.response,
                         message=replace(update.response.message, replay=None)))
    runtime.endpoint_invoker.response_hooks = SimpleNamespace(normalize=normalize)
    executor = _executor(memory, runtime)
    prior = []
    for index in range(100):
        _drive(executor, _request_for(memory, ()))
        sent = thaw_json(transport.captured[-1].payload)["messages"]
        assert sent[:len(prior)] == prior
        prior = sent
        accepted = memory.active_l1_turn("T").messages[-1]
        assert accepted.reasoning_text == f"REASON-{index}"
        assert thaw_json(accepted.replay.payload["message"]) == native_message(index)
        memory.append_l1_tool_result("T", ToolResultIR(
            call_id=f"call-{index}", name="read_file", content=f"full result {index}"))
    memory.settle_l1_turn("T")
    before = tuple(m for t in memory.l1_store.turns.turns for m in t.messages)
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(MemoryRuntimeStatePort(memory).snapshot_state()))
    after = tuple(m for t in restored.l1_store.turns.turns for m in t.messages)
    assert after == before
    rebuilt_runtime = _runtime(NativeTransport())
    pack = _executor(restored, rebuilt_runtime)._prepare_turn_projection(rebuilt_runtime, _request_for(restored, ()))
    assert pack is not None
    body = thaw_json(pack[0].payload)["messages"]
    assert body[:len(prior)] == prior
    replayed = [m for m in body if m.get("reasoning_content")]
    assert replayed == [native_message(i) for i in range(100)]


def test_admission_survives_a_later_transport_failure():
    class Transport:
        def frames(self, endpoint, request):
            request.on_submitted()
            raise RuntimeError("admitted, then disconnected")
            yield
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("q", "q"))
    executor = _executor(memory, _runtime(Transport()))
    executor._handle_llm_provider_errors = False
    _drive(executor, _request_for(memory, ()))
    assert [m.message_id for m in memory.history_root.left_messages()] == ["q"]
    assert not memory.history_root.right_messages()


def test_manual_archive_does_not_absorb_an_active_unsent_tail():
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("q", "q"))
    root = memory.history_root
    root.commit_submission(root.prepare_submission(("q",)), ("q",))
    reply = LLMMessageIR(MessageRole.ASSISTANT, (ReasoningPartIR("r"), TextPartIR("a")))
    memory.upsert_l1_assistant("T", reply)
    root.promote()
    assert root.right_messages() == (reply,)
    memory.settle_l1_turn("T")
    root.promote()
    assert not root.right_messages()
    assert root.left_messages()[-1] == reply


def test_sdk_nonstream_admission_precedes_response_body_parsing():
    import httpx
    from openai import OpenAI
    from pal.llm.transport import SDKJSONTransport, SDKTransportRequest
    seen = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            assert seen == ["submitted"]
            yield json.dumps({"choices": [{"message": native_message(), "finish_reason": "tool_calls"}]}).encode()
    client = OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=Body())
    )))
    transport = SDKJSONTransport(client_factory=SimpleNamespace(openai=lambda **_: client))
    request = SDKTransportRequest("request", "endpoint", WireShape.OPENAI_COMPLETION,
        "test", "https://example.test", 5, {"model": "model", "messages": []}, False,
        on_submitted=lambda: seen.append("submitted"))
    try:
        frames = list(transport.frames(request))
        assert frames[0].payload["choices"][0]["message"]["content"] is None
        assert seen == ["submitted"]
    finally:
        transport.close()


def test_responses_final_items_supply_opaque_fields_missing_from_deltas():
    shape = WireShape.OPENAI_RESPONSE
    output = [
        {"id": "reason", "type": "reasoning", "encrypted_content": "opaque", "summary": []},
        {"id": "answer", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]},
    ]
    frames = [
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "reason", "type": "reasoning"}},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "a"},
        {"type": "response.completed", "response": {"output": output}},
    ]
    updates = list(codec_for_shape(shape).decode(
        iter(_JSONFrame(i, p) for i, p in enumerate(frames)), ShapeContext(shape, "ep", "model")))
    assert thaw_json(updates[-1].response.message.replay.payload["output"]) == output


def test_hook_adaptation_keeps_original_source_and_a_single_callable_inventory():
    from pal.llm.replay_acceptance import bind_accepted_replay
    from pal.llm.ir import ReplayEnvelope
    from pal.shared.tool_protocol import ToolCallIR
    raw = {"message": {"role": "assistant", "content": "original textual tool syntax", "reasoning_content": "r"}}
    message = LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("c", "read", {"path": "x"}),))
    accepted = bind_accepted_replay(message, ReplayEnvelope(WireShape.OPENAI_COMPLETION, "ep", "m", raw), provider_id="deepseek")
    assert thaw_json(accepted.replay.source_payload) == raw
    assert len(accepted.replay.payload["message"]["tool_calls"]) == 1
    assert accepted.replay.payload["message"]["reasoning_content"] == "r"
    assert message_from_payload(message_to_payload(accepted)) == accepted


def test_recovery_keeps_all_native_reasoning_instead_of_only_last_response():
    from pal.llm.ir import LLMResponseIR, ReplayEnvelope
    from pal.llm.output_recovery import merge_responses
    responses = [LLMResponseIR(LLMMessageIR(MessageRole.ASSISTANT,
        (ReasoningPartIR(f"r{i}"), TextPartIR(str(i))),
        replay=ReplayEnvelope(WireShape.OPENAI_COMPLETION, "ep", "m", {
            "message": {"role": "assistant", "content": str(i), "reasoning_content": f"r{i}"}
        })), finish_reason="stop") for i in range(2)]
    merged = merge_responses(responses)
    assert merged.message.reasoning_text == "r0r1"
    assert [item["reasoning_content"] for item in merged.message.replay.payload["messages"]] == ["r0", "r1"]
    assert message_from_payload(message_to_payload(merged.message)) == merged.message


def test_compact_rebases_admitted_input_without_losing_unsent_native_output():
    from tests.test_v3_n1_root_lifecycle import Candidate
    from tests.test_v3_4b14ce4_review_fixes import _model_view_request

    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("old question", "q"))
    runtime = _runtime(NativeTransport())
    executor = _executor(memory, runtime)
    try:
        _drive(executor, _request_for(memory, ()))
        memory.append_l1_tool_result("T", ToolResultIR(
            call_id="call-0", name="read_file", content="full result in R"))
        right = memory.history_root.right_messages()
        root = memory.history_root
        root.begin_compact("compact", reason="test")
        root.mark_ready("compact", Candidate())
        root.commit("compact")
        assert root.right_messages() == right
        executor._rebase_projection_after_left_install(
            runtime, "pal:resident", root, memory_service=memory)
        assert not executor.state.diagnostics
        warm = executor._prepare_turn_projection(runtime, _model_view_request(memory))
        assert warm is not None
        warm_wire = thaw_json(warm[0].payload)["messages"]
        assert native_message() in warm_wire

        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(
            MemoryRuntimeStatePort(memory).snapshot_state()))
        cold_runtime = _runtime(NativeTransport())
        try:
            cold = _executor(restored, cold_runtime)._prepare_turn_projection(
                cold_runtime, _model_view_request(restored))
            assert cold is not None
            assert thaw_json(cold[0].payload)["messages"] == warm_wire
        finally:
            cold_runtime.close()
    finally:
        runtime.close()


def test_completion_null_deltas_do_not_erase_accepted_tool_or_text():
    ctx = ShapeContext(WireShape.OPENAI_COMPLETION, "endpoint", "model")
    deltas = [
        {"role": "assistant", "content": "Checking", "reasoning_content": "Think",
         "tool_calls": [{"index": 0, "id": "call", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":'}}]},
        {"role": None, "content": None, "reasoning_content": None,
         "tool_calls": [{"index": 0, "id": None, "type": None,
                         "function": {"name": None, "arguments": '"x"}'}}]},
        {"tool_calls": [{"index": 0, "function": None}]},
        {"tool_calls": None},
    ]
    frames = [_JSONFrame(i, {"choices": [{"delta": d}]}) for i, d in enumerate(deltas)]
    frames.append(_JSONFrame(len(frames), {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
    message = list(codec_for_shape(ctx.wire_shape).decode(iter(frames), ctx))[-1].response.message
    from pal.llm.replay_acceptance import validate_native_for_send
    validate_native_for_send(message)
    assert message.replay.payload["message"]["role"] == "assistant"
    assert message.replay.payload["message"]["content"] == message.text == "Checking"
    assert message.replay.payload["message"]["reasoning_content"] == "Think"
    assert message.replay.payload["message"]["tool_calls"][0]["function"] == {
        "name": "read_file", "arguments": '{"path":"x"}'}


def test_completion_reasoning_details_keep_every_streamed_block_after_restore():
    ctx = ShapeContext(WireShape.OPENAI_COMPLETION, "endpoint", "model")
    blocks = [
        {"type": "reasoning.text", "index": 0, "id": "r", "text": "first ", "signature": None},
        {"type": "reasoning.text", "index": 0, "id": "r", "text": "second", "signature": "signed"},
        {"type": "reasoning.encrypted", "index": 1, "data": "opaque"},
    ]
    deltas = [{"reasoning_details": [block]} for block in blocks]
    deltas.extend([{"reasoning_details": []}, {"reasoning_details": None}, {"content": "done"}])
    frames = [_JSONFrame(i, {"choices": [{"delta": d}]}) for i, d in enumerate(deltas)]
    frames.append(_JSONFrame(len(frames), {"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    message = list(codec_for_shape(ctx.wire_shape).decode(iter(frames), ctx))[-1].response.message
    restored = message_from_payload(message_to_payload(message))
    from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
    wire = codec_for_shape(ctx.wire_shape).encode(
        LLMRequestIR(messages=(restored,), tools=(), policy=GenerationPolicyIR(max_output_tokens=1024)), ctx)
    assert thaw_json(wire.payload["messages"][0]["reasoning_details"]) == blocks


def test_main_schema_one_snapshot_migrates_closed_history_without_losing_active_tail():
    from pal.core.module_registry import ModuleHandle, ModuleRegistry
    from pal.core.runtime_state import RuntimeSnapshotCoordinator, RuntimeSnapshotIdentity

    memory = MemoryService()
    memory.begin_l1_turn("old", user_message=user("previous", "old-q"))
    memory.upsert_l1_assistant("old", LLMMessageIR(
        MessageRole.ASSISTANT, (TextPartIR("answer"),), message_id="old-a"))
    memory.settle_l1_turn("old")
    memory.begin_l1_turn("active", user_message=user("new work", "new-q"))
    port = MemoryRuntimeStatePort(memory)
    registry = ModuleRegistry()
    registry.register(ModuleHandle(module_id="memory", tier="test", runtime_state_port=port))
    coordinator = RuntimeSnapshotCoordinator(registry)
    snapshot = asyncio.run(coordinator.snapshot(RuntimeSnapshotIdentity(
        logical_coroutine_id="resident", workflow_id="test", stage_key="turn",
        sequence=1, producer_fencing_token=1, runtime_spec_hash="test")))
    record = snapshot["modules"]["memory"]
    record["schema_version"] = "1"
    for key in ("history_root", "context_epoch", "compaction_receipts"):
        record["payload"].pop(key)
    memory.soft_reset()
    asyncio.run(coordinator.restore(snapshot))
    assert [message.message_id for message in memory.history_root.left_messages()] == ["old-q", "old-a"]
    assert [message.message_id for message in memory.history_root.right_messages()] == ["new-q"]


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "reasoning_details"])
def test_native_reasoning_aliases_keep_model_binding_guard(field):
    from pal.llm.replay_acceptance import has_opaque_continuation
    ctx = ShapeContext(WireShape.OPENAI_COMPLETION, "endpoint", "model")
    value = [{"type": "reasoning.encrypted", "data": "opaque"}] if field == "reasoning_details" else "think"
    frames = iter([_JSONFrame(0, {"choices": [{"message": {
        "role": "assistant", "content": "done", field: value}, "finish_reason": "stop"}]})])
    message = list(codec_for_shape(ctx.wire_shape).decode(frames, ctx))[-1].response.message
    assert has_opaque_continuation(message_from_payload(message_to_payload(message)))
