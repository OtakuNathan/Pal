"""Native compaction keeps canonical history and the owner's failure semantics."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.core.compaction import CompactionClockKind, CompactionEngine, CompactionSnapshot
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm.compaction import NATIVE_WINDOW_KEY, is_native_window, native_origin
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import (GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
                        MessageState, ReplayEnvelope, TextPartIR, WireShape)
from pal.llm.model_adapter import bind_model_adapter
from pal.llm.model_hooks import ModelHookRegistry
from pal.llm.replay_acceptance import validate_native_for_send
from pal.llm.runtime import EndpointResolver, LLMRuntime, LLMRequestPreparationError
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport import DirectEndpointTransport
from pal.memory import MemoryService
from pal.memory.compaction_candidate import CompactionCandidate
from pal.memory.history_root import HistoryRoot, LeftChangedSinceCapture
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from tests.test_model_switch import endpoint, Settings
from tests.test_runtime_compaction import _valid_pal_payload


def native_output():
    return [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "remember 47"}],
         "future_field": {"keep": [1, 2]}},
        {"type": "function_call", "call_id": "historical", "name": "never_execute", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "historical", "output": "done"},
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "OPAQUE" * 1000, "future": True},
    ]


class OperationTransport:
    def __init__(self):
        self.requests = []
        self.error = False
        self.count = 41

    def frames(self, endpoint, request):
        self.requests.append(request)
        if self.error and request.operation == "compact":
            raise RuntimeError("provider unavailable")
        if request.operation == "input_tokens":
            yield _JSONFrame(0, {"object": "response.input_tokens", "input_tokens": self.count})
        elif request.operation == "compact":
            yield _JSONFrame(0, {"output": native_output(), "usage": {"input_tokens": 80, "output_tokens": 41}})
        else:
            yield _JSONFrame(0, {"id": "resp", "status": "completed", "output": [
                {"type": "message", "id": "m", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": _valid_pal_payload("handoff 47"), "annotations": []}]}]})


def runtime():
    ep = endpoint(model="gpt-6-luna")
    transport = OperationTransport()
    invoker = ShapeEndpointInvoker(transport=transport)
    rt = LLMRuntime(EndpointResolver(endpoints=(ep, endpoint("b", "another-model"))),
                    Settings(), endpoint_invoker=invoker)
    return rt, ep, transport


def request(*messages):
    return LLMRequestIR(messages=messages, tools=(), policy=GenerationPolicyIR(max_output_tokens=512))


def user(text="hello", **kwargs):
    return LLMMessageIR(MessageRole.USER, (TextPartIR(text),), **kwargs)


def assistant(text="answer", **kwargs):
    return LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR(text),), **kwargs)


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra"])
def test_verified_official_models_bind_native(model):
    assert bind_model_adapter(LLMEndpointSpec.from_value(endpoint(model=model))).compaction_strategy == "openai_responses"


@pytest.mark.parametrize("changes", [
    {"model_id": "gpt-6.2-unknown"}, {"model_id": "glm-5.3"}, {"model_id": "deepseek-v4.1-pro"},
    {"model_id": "gemini-4-argon"}, {"base_url": "https://openrouter.ai/api/v1"},
    {"transport": "websocket"}, {"auth_kind": "oauth"},
])
def test_unverified_routes_fall_back_and_reject_forced_native(changes):
    ep = endpoint(model="gpt-6-luna", **changes)
    assert bind_model_adapter(LLMEndpointSpec.from_value(ep)).compaction_strategy == "semantic_checkpoint"
    ep.capabilities_blob["compaction_strategy"] = "openai_responses"
    with pytest.raises(ValueError, match="requires a verified"):
        bind_model_adapter(LLMEndpointSpec.from_value(ep))


def test_legacy_and_adapter_exports_share_one_binding(tmp_path):
    directory = tmp_path / "llm" / "models"
    directory.mkdir(parents=True)
    path = directory / "model.py"
    path.write_text('from pal.llm.model_adapter import ModelAdapter\nMODEL_ADAPTER = ModelAdapter("gpt-6-luna", developer_instructions=("compact hook",), compaction_strategy="semantic_checkpoint")')
    registry = ModelHookRegistry.load(tmp_path)
    ep = LLMEndpointSpec.from_value(endpoint(model="gpt-6-luna"))
    bound = bind_model_adapter(ep, registry)
    assert bound.compaction_strategy == "semantic_checkpoint"
    assert bound.apply(request(user())).messages[0].text == "compact hook"
    path.write_text('MODEL_ID = "gpt-6-luna"\nCOMPACTION_STRATEGY = "auto"')
    successor = bind_model_adapter(ep, ModelHookRegistry.load(tmp_path))
    assert successor.identity != bound.identity
    assert successor.compaction_strategy == "openai_responses"
    assert bound.compaction_strategy == "semantic_checkpoint"


def test_native_output_is_canonical_not_executable_and_counted_exactly():
    rt, ep, transport = runtime()
    replay, tokens, usage = asyncio.run(rt.acompact_native(request(user(), assistant())))
    assert tokens == 41
    assert thaw_json(replay.payload["output"]) == native_output()
    seed = LLMMessageIR(MessageRole.ASSISTANT, replay=replay)
    validate_native_for_send(seed)
    assert seed.tool_calls == ()
    wire = rt.endpoint_invoker.encode_request(ep, request(seed, user("continue"))).payload
    assert thaw_json(wire["input"][:-1]) == native_output()
    prepared = rt._compile_request(ep, request(seed))
    assert prepared.estimated_input_tokens == 41  # never len(ciphertext) / 4
    before = len(transport.requests)
    rt._compile_request(ep, request(seed))
    assert len(transport.requests) == before  # exact request digest cache
    compact = next(r for r in transport.requests if r.operation == "compact")
    assert set(compact.payload) == {"model", "input"}
    assert usage["input_tokens"] == 80
    with pytest.raises(LLMRequestPreparationError, match="another model"):
        rt._compile_request(rt.endpoint_resolver.endpoints[1], request(seed))


def test_canonical_prefix_is_not_duplicated_on_replay_or_recompact():
    rt, ep, transport = runtime()
    preamble = LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("stable system"),))
    replay, _, _ = asyncio.run(rt.acompact_native(request(preamble, user(), assistant())))
    seed = LLMMessageIR(MessageRole.ASSISTANT, replay=replay)
    wire = rt.endpoint_invoker.encode_request(ep, request(preamble, seed, user("next"))).payload
    assert thaw_json(wire["input"][:-1]) == native_output()
    asyncio.run(rt.acompact_native(request(preamble, seed, user("next"), assistant())))
    compact = [r for r in transport.requests if r.operation == "compact"][-1]
    assert thaw_json(compact.payload["input"][:len(native_output())]) == native_output()


def source_memory():
    memory = MemoryService()
    root = memory.history_root
    root.store.append(L1TurnIR("old", messages=(user(), assistant()), state=L1TurnState.SETTLED))
    root.promote()
    call = ToolCallIR(call_id="c1", name="read", arguments={})
    root.store.append(L1TurnIR("active", messages=(
        user("task"), LLMMessageIR(MessageRole.ASSISTANT, (call,)),
        LLMMessageIR(MessageRole.TOOL, (ToolResultIR(call_id="c1", name="read", content="done"),)),
        user("pending"),
    )))
    return memory


def snapshot(memory, *, native=True):
    left = memory.begin_left_compaction("test", reason="test", include_completed_tail=native)
    canonical = request(*(m for turn in left.turns for m in turn.messages))
    snap = CompactionSnapshot.capture_left(memory, left,
        target_input_budget=1000, reserved_output_tokens=512, clock_kind=CompactionClockKind.USER_TURN,
        clock_value=1, replay_request=canonical, replay_wire_shape="openai_response",
        metadata={"compaction_strategy": "openai_responses" if native else "semantic_checkpoint",
                  "compact_source_ids": left.message_ids(), "retained_result_call_ids": ["c1"]})
    return left, snap


def test_capture_completed_tail_is_prospective_and_cancel_keeps_both_segments():
    memory = source_memory()
    root = memory.history_root
    cut, turns = root.cut, root.all_turns()
    left, _ = snapshot(memory)
    assert root.cut == cut
    assert left.cut.intra_messages == 3
    assert left.turns[-1].messages[-1].role == MessageRole.TOOL
    root.cancel("test")
    assert root.cut == cut and root.all_turns() == turns


def test_native_commit_recovery_and_semantic_handoff_share_one_window():
    rt, ep, transport = runtime()
    memory = source_memory()
    left, snap = snapshot(memory)
    pending = memory.history_root.right_messages()[-1]
    engine = CompactionEngine(PalCompactionPolicy(), max_attempts=1)
    result = asyncio.run(engine.run(snap, llm_runtime=rt, memory_service=memory))
    assert result.success, result.failures
    assert result.candidate.native_window is not None and result.candidate.checkpoint is None
    assert not result.summary_entry.payload.get("memory_candidates")
    assert memory.history_root.right_messages() == (pending,)
    assert memory.history.turns[0].metadata["compact_retained_result_refs"] == ("c1",)
    state = MemoryRuntimeStatePort(memory).snapshot_state()
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    prepared = port.prepare_restore_state(state)
    port.install_prepared_state(prepared)
    seed = restored.history.continuity.standalone_message()
    assert is_native_window(seed)
    assert thaw_json(seed.replay.payload["output"]) == native_output()
    assert not seed.tool_calls
    # Handoff stays on A, feeds opaque history through the normal codec, and
    # replaces it with the existing portable text policy before B is selected.
    left = restored.begin_left_compaction("handoff", reason="model_switch", allow_summary=True)
    handoff = CompactionSnapshot.capture_left(restored, left, target_input_budget=1000,
        reserved_output_tokens=512, clock_kind=CompactionClockKind.USER_TURN, clock_value=2,
        replay_request=request(seed), replay_wire_shape="openai_response")
    result = asyncio.run(engine.run(handoff, llm_runtime=rt, memory_service=restored))
    assert result.success, result.failures
    assert restored.history.continuity.native_window is None
    sent = [r for r in transport.requests if r.operation == "generate"][-1]
    assert thaw_json(sent.payload["input"][:len(native_output())]) == native_output()
    assert rt.active_endpoint_id == "a"


@pytest.mark.parametrize("failure", ["provider", "budget", "cancel"])
def test_native_failure_never_falls_back_or_mutates_history(failure):
    rt, ep, transport = runtime()
    memory = source_memory()
    before = memory.history_root.all_turns(), memory.history_root.cut
    _, snap = snapshot(memory)
    if failure == "provider":
        transport.error = True
    elif failure == "budget":
        transport.count = 2000
    result = asyncio.run(CompactionEngine(PalCompactionPolicy(), max_attempts=2).run(
        snap, llm_runtime=rt, memory_service=memory,
        commit_guard=lambda: failure != "cancel"))
    assert not result.success
    assert (memory.history_root.all_turns(), memory.history_root.cut) == before
    assert not any(r.operation == "generate" for r in transport.requests)


def test_changed_captured_tail_rejects_install_but_new_right_growth_survives():
    memory = source_memory()
    left, _ = snapshot(memory)
    turn = memory.history.get("active")
    memory.history_root.store.replace(replace(turn, revision=turn.revision + 1, messages=(
        *turn.messages[:2], replace(turn.messages[2], metadata={"changed": True}), *turn.messages[3:])))
    replay = ReplayEnvelope(WireShape.OPENAI_RESPONSE, "a", "gpt-6-luna",
        {"output": native_output(), NATIVE_WINDOW_KEY: {"version": 1, "origin": "test"}})
    memory.history_root.mark_ready("test", CompactionCandidate(native_window=replay).as_entry())
    with pytest.raises(LeftChangedSinceCapture):
        memory.history_root.commit("test")


def test_incomplete_messages_and_unpaired_calls_stay_outside_capture():
    for tail in (assistant(state=MessageState.INCOMPLETE), LLMMessageIR(MessageRole.ASSISTANT,
                (ToolCallIR(call_id="pending", name="write", arguments={}),))):
        root = HistoryRoot()
        root.store.append(L1TurnIR("active", messages=(user(), assistant(), tail)))
        left = root.begin_compact("r", reason="native", include_completed_tail=True)
        assert left.cut.intra_messages == 2
        assert root.cut.turn_count == 0


def test_executor_uses_native_strategy_and_then_forces_text_handoff():
    from tests.test_control_plane import PalControlFlowTests

    async def scenario():
        fixture = PalControlFlowTests()
        await fixture.asyncSetUp()
        rt, ep, transport = runtime()
        fixture.core.context.port_registry["llm:llm"] = rt
        memory = fixture.memory_service
        memory.begin_l1_turn("old", user_text="remember 47")
        memory.upsert_l1_assistant("old", assistant("remembered"))
        memory.settle_l1_turn("old")
        executor = fixture.core.turn_executor
        result = await executor.compact_memory_async(memory, target_input_budget=20000,
            reserved_output_tokens=512, max_attempts=1)
        assert result.success, result.failures
        assert memory.history.continuity.native_window is not None
        assert not any(r.operation == "generate" for r in transport.requests)
        result = await executor.compact_memory_async(memory, target_input_budget=20000,
            reserved_output_tokens=512, max_attempts=1, force_semantic=True)
        assert result.success, result.failures
        assert memory.history.continuity.native_window is None
        assert rt.active_endpoint_id == "a"

    asyncio.run(scenario())


def test_next_input_count_includes_pending_tail_and_tool_definitions():
    from pal.shared.tool_protocol import ToolDefinitionIR
    rt, ep, transport = runtime()
    next_request = replace(request(user("PENDING_SENTINEL")), tools=(
        ToolDefinitionIR(name="read", description="read", input_schema={"type": "object"}),))
    asyncio.run(rt.acompact_native(request(user(), assistant()), next_request=next_request))
    compact = next(r for r in transport.requests if r.operation == "compact")
    assert "PENDING_SENTINEL" not in json.dumps(thaw_json(compact.payload))
    counted = [r for r in transport.requests if r.operation == "input_tokens"][-1]
    assert "PENDING_SENTINEL" in json.dumps(thaw_json(counted.payload))
    assert counted.payload["tools"][0]["name"] == "read"


def test_native_source_failure_closes_executor_run_without_moving_cut():
    from tests.test_control_plane import PalControlFlowTests

    async def scenario():
        fixture = PalControlFlowTests()
        await fixture.asyncSetUp()
        rt, _, transport = runtime()
        fixture.core.context.port_registry["llm:llm"] = rt
        memory = fixture.memory_service
        memory.begin_l1_turn("old", user_text="hello")
        memory.upsert_l1_assistant("old", assistant())
        memory.settle_l1_turn("old")
        cut = memory.history_root.cut
        with patch.object(fixture.core.turn_executor, "_build_canonical_prompt", side_effect=ValueError("source failed")):
            result = await fixture.core.turn_executor.compact_memory_async(memory,
                target_input_budget=20000, reserved_output_tokens=512, max_attempts=1)
        assert result.status == "source_unavailable"
        assert memory.history_root.active_run is None
        assert memory.history_root.cut == cut
        assert not transport.requests

    asyncio.run(scenario())


def test_auxiliary_sdk_operation_preserves_raw_unknown_fields():
    from pal.llm.http_transport import _SDKClientGraph, _open_sdk_response, _next_sdk_frame, _close_active_sdk_response
    from pal.llm.transport_contracts import AuthenticatedTransportRequest

    class Raw:
        def json(self):
            return {"output": native_output()}

        def parse(self):
            raise AssertionError("opaque history must not pass through SDK union conversion")

    class Manager:
        closed = False

        def __enter__(self):
            return Raw()

        def __exit__(self, *args):
            self.closed = True

    manager = Manager()
    seen = []
    def compact(**payload):
        seen.append(payload)
        return manager

    graph = _SDKClientGraph(client=None, resource=SimpleNamespace(
        with_streaming_response=SimpleNamespace(compact=compact)))
    req = AuthenticatedTransportRequest("r", "a", WireShape.OPENAI_RESPONSE, "test-only",
        "https://api.openai.com/v1", 5, {}, False, operation="compact")
    _open_sdk_response(graph, req, {"model": "gpt-6-luna", "input": []})
    assert thaw_json(_next_sdk_frame(graph).payload["output"]) == native_output()
    assert _close_active_sdk_response(graph) and manager.closed
    assert "stream" not in seen[0]


def test_cancel_interrupts_native_worker_without_a_followup_count():
    from threading import Event
    rt, ep, transport = runtime()
    entered, release = Event(), Event()
    original = transport.frames

    def frames(endpoint, req):
        if req.operation == "compact":
            entered.set()
            release.wait(5)
            req.stream_control.raise_if_cancelled()
        yield from original(endpoint, req)

    transport.frames = frames

    async def scenario():
        task = asyncio.create_task(rt.acompact_native(request(user(), assistant())))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.gather(*rt._detached_stream_tasks, return_exceptions=True)
        assert len(transport.requests) == 1  # source count only; no installed output

    asyncio.run(scenario())


def test_failed_refresh_keeps_the_previous_adapter_and_endpoint_set():
    rt, ep, transport = runtime()
    old = rt.model_adapters["a"]
    invalid = endpoint(model="unverified", capabilities_blob={"compaction_strategy": "openai_responses"})
    rt.endpoint_resolver.repository = SimpleNamespace(list_enabled=lambda: [invalid])
    with pytest.raises(ValueError):
        rt.refresh_llm_endpoints()
    assert rt.model_adapters["a"] is old
    assert rt.endpoint_resolver.endpoints[0] is ep


def test_settings_refresh_does_not_bypass_the_model_handoff():
    rt, ep, transport = runtime()
    rt.settings_repository.active = "b"
    rt.refresh_runtime_settings()
    assert rt.active_endpoint_id == "a"
    rt.apply_model_selection("b", "low")
    assert rt.active_endpoint_id == "b"


def test_native_snapshot_files_survive_until_semantic_handoff_or_reset(tmp_path):
    from pal.execution.result_snapshots import ResultSnapshotStore
    from tests.test_result_snapshots import result_turn
    rt, ep, transport = runtime()
    memory = MemoryService()
    files = ResultSnapshotStore(tmp_path)
    files.bind_history(memory.history)
    ref = files.capture("retained tool output", call_id="file", lifetime="test")
    memory.history_root.store.append(result_turn(ref))
    files.finish_references((ref,))
    left, snap = snapshot(memory)
    engine = CompactionEngine(PalCompactionPolicy(), max_attempts=1)
    result = asyncio.run(engine.run(snap, llm_runtime=rt, memory_service=memory))
    assert result.success, result.failures
    assert memory.history.turns[0].messages[0].metadata["result_snapshots"][0]["snapshot_id"] == ref.snapshot_id
    assert Path(ref.path).read_text() == "retained tool output"
    seed = memory.history.continuity.standalone_message()
    left = memory.begin_left_compaction("handoff", reason="model_switch", allow_summary=True)
    snap = CompactionSnapshot.capture_left(memory, left, target_input_budget=1000,
        reserved_output_tokens=512, clock_kind=CompactionClockKind.USER_TURN, clock_value=2,
        replay_request=request(seed), replay_wire_shape="openai_response")
    result = asyncio.run(engine.run(snap, llm_runtime=rt, memory_service=memory))
    assert result.success, result.failures
    assert not Path(ref.path).exists()
