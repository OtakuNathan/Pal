"""Native compaction keeps canonical history and the owner's failure semantics."""
from tests.compaction_composition import execute_compaction
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.llm.compaction_engine import CompactionClockKind, CompactionEngine, CompactionSnapshot
from pal.llm.resident_compaction import PalCompactionPolicy
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


def runtime(model="gpt-6-luna", **options):
    ep = endpoint(model=model, transport="http", **options)
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


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6.2-astra", "gpt-6.2-sol"])
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


def test_native_compact_uses_chase_tail_on_its_own_schema():
    from pal.llm.cache_wire import protocol_nodes
    from pal.llm.ir import PromptRegionIR
    rt, ep, transport = runtime(model="gpt-6.2-sol", capabilities_blob={"prompt_cache": {"mode": "explicit"}})
    system = LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("Stable instructions"),), prompt_region=PromptRegionIR.STABLE_SYSTEM)
    source = replace(request(system, *(user(f"record {i}", prompt_region=PromptRegionIR.SETTLED_HISTORY) for i in range(60))),
                     logical_scope_id="pal:resident", request_instructions="Temporary reminder")
    rt_meta = rt.metadata()
    assert rt_meta.supports_suffix_instructions
    asyncio.run(rt.acompact_native(source))
    compact, = [r for r in transport.requests if r.operation == "compact"]
    assert set(compact.payload) == {"model", "input"}
    assert len([path for path, node in protocol_nodes(thaw_json(compact.payload)) if "prompt_cache_breakpoint" in node]) == 52
    assert compact.extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    prepared = rt._compile_request(ep, source, count_native=False)
    invoker = rt.endpoint_invoker
    raw = invoker.encode_request(ep, prepared.request)
    from pal.llm.shapes.base import ShapeContext
    ctx = ShapeContext(WireShape.OPENAI_RESPONSE, ep.endpoint_id, ep.model_id, provider_id=ep.provider, base_url=ep.base_url, capabilities=ep.capabilities_blob)
    normal = invoker.prompt_cache.plan(prepared.request, ctx, raw)
    assert normal.cache_key == compact.extra_body["prompt_cache_key"]
    assert all(p.path[0] == "input" for p in normal.breakpoints)
    assert raw.payload["instructions"] == "Temporary reminder"


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
    snap = memory.capture_compaction( left,
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
    result = asyncio.run(execute_compaction(engine, snap, llm_runtime=rt, memory_service=memory))
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
    handoff = restored.capture_compaction( left, target_input_budget=1000,
        reserved_output_tokens=512, clock_kind=CompactionClockKind.USER_TURN, clock_value=2,
        replay_request=request(seed), replay_wire_shape="openai_response")
    result = asyncio.run(execute_compaction(engine, handoff, llm_runtime=rt, memory_service=restored))
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
    result = asyncio.run(execute_compaction(CompactionEngine(PalCompactionPolicy(), max_attempts=2),
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
        result = await executor.compaction.run(memory, target_input_budget=20000,
            reserved_output_tokens=512, max_attempts=1)
        assert result.success, result.failures
        assert memory.history.continuity.native_window is not None
        assert not any(r.operation == "generate" for r in transport.requests)
        result = await executor.compaction.run(memory, target_input_budget=20000,
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
            result = await fixture.core.turn_executor.compaction.run(memory,
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


@pytest.mark.parametrize("blocked_operation", ["input_tokens", "compact"])
def test_cancel_interrupts_native_worker_without_a_followup_count(blocked_operation):
    from threading import Event
    rt, ep, transport = runtime()
    entered, release = Event(), Event()
    original = transport.frames

    def frames(endpoint, req):
        if req.operation == blocked_operation:
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
        expected = ["input_tokens"] if blocked_operation == "compact" else []
        assert [req.operation for req in transport.requests] == expected

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
    result = asyncio.run(execute_compaction(engine, snap, llm_runtime=rt, memory_service=memory))
    assert result.success, result.failures
    assert memory.history.turns[0].messages[0].metadata["result_snapshots"][0]["snapshot_id"] == ref.snapshot_id
    assert Path(ref.path).read_text() == "retained tool output"
    seed = memory.history.continuity.standalone_message()
    left = memory.begin_left_compaction("handoff", reason="model_switch", allow_summary=True)
    snap = memory.capture_compaction( left, target_input_budget=1000,
        reserved_output_tokens=512, clock_kind=CompactionClockKind.USER_TURN, clock_value=2,
        replay_request=request(seed), replay_wire_shape="openai_response")
    result = asyncio.run(execute_compaction(engine, snap, llm_runtime=rt, memory_service=memory))
    assert result.success, result.failures
    assert not Path(ref.path).exists()


@pytest.mark.parametrize(("count", "compact"), [(272_000, False), (272_001, True)])
def test_sol_watermark_admits_full_standard_tier(count, compact):
    rt, ep, transport = runtime("gpt-6.2-sol", context_window=320_384, max_output_tokens=128_000)
    transport.count = count
    prompt = replace(request(user("x" * 1_066_000)), policy=GenerationPolicyIR(max_output_tokens=128_000))
    prepared = rt._compile_request(ep, prompt)
    assert prepared.request.policy.max_output_tokens == 65_535
    assert prepared.target_input_budget == 272_000
    assert prepared.compact_required == compact
    assert prepared.budget_details["budget_source"] == "provider"
    assert [r.operation for r in transport.requests] == ["input_tokens"]
    ep.capabilities_blob["allow_long_context_pricing"] = True
    assert rt._target_input_budget(ep, 65_535) == 320_384
    ep.capabilities_blob["physical_context_window"] = 300_000
    assert rt._target_input_budget(ep, 65_535) == 300_000 - 65_535 - 15_000


@pytest.mark.parametrize("generation_submitted", [False, True])
def test_websocket_count_over_https_preserves_generation_retry_boundary(generation_submitted):
    from pal.llm.context_budget import standard_input_limit, provider_input_maximum
    from pal.llm.transport import LLMStreamControl
    from tests.test_chatgpt_websocket import _completed

    ep = endpoint(model="gpt-6.2-sol", transport="websocket", base_url="wss://api.openai.com/v1/responses")
    calls = []
    control = LLMStreamControl()

    class HTTP:
        def frames(self, req):
            calls.append(req.operation)
            assert req.base_url == "https://api.openai.com/v1"
            assert req.api_key == "test" and not req.stream
            assert req.stream_control is not control
            req.stream_control.mark_provider_started()
            yield _JSONFrame(0, {"input_tokens": 10_000})

    class Socket:
        def frames(self, req):
            assert req.base_url == ep.base_url and req.api_key == "test"
            assert req.stream_control is control and not control.provider_started
            calls.append(req.operation)
            if calls.count("generate") == 1:
                if generation_submitted:
                    control.mark_provider_started()
                raise ConnectionError("connection reset")
            yield _JSONFrame(0, _completed())

        def close(self):
            pass

    transport = DirectEndpointTransport(lambda _: "test", HTTP(), Socket())
    rt = LLMRuntime(EndpointResolver(endpoints=(ep,)), Settings(),
                    endpoint_invoker=ShapeEndpointInvoker(transport=transport))
    try:
        assert standard_input_limit(ep) == 272_000
        assert provider_input_maximum(ep) == 922_000
        with patch("pal.llm.runtime._retry_delay", return_value=0):
            updates = list(rt._iter_stream_updates(request(user("x" * 420_000)), stream_control=control))
        if generation_submitted:
            assert calls == ["input_tokens", "generate"]
            assert updates[-1].response.finish_reason == "error"
        else:
            assert calls == ["input_tokens", "generate", "generate"]
            assert updates[-1].response.finish_reason == "stop"
    finally:
        rt.close()


def test_standard_tier_on_openrouter_skips_remote_counter():
    from pal.llm.context_budget import counting_shape, standard_input_limit
    rt, ep, transport = runtime("openai/gpt-6.2-astra", context_window=1_050_000, base_url="https://openrouter.ai/api/v1")
    assert standard_input_limit(ep) == 272_000
    assert counting_shape(ep) is None
    assert rt._compile_request(ep, request(user("x" * 1_088_004))).compact_required
    assert transport.requests == []
    ep.model_id += "-alias"
    assert standard_input_limit(ep) is None
    ep.base_url = "https://example.com/v1"
    ep.model_id = "gpt-6.2-sol"
    assert counting_shape(ep) is None
    assert standard_input_limit(ep) is None


def test_usage_baseline_covers_matching_prefix_and_estimates_only_new_messages():
    from pal.llm.ir import LLMResponseIR, LLMFinishReason, LLMUsageIR
    from pal.shared.tool_protocol import ToolDefinitionIR
    rt, ep, transport = runtime()
    ep.context_window = 320_384
    tool = ToolDefinitionIR("read", "read", {"type": "object"})
    initial = replace(request(user("history")), tools=(tool,), logical_scope_id="resident")
    initial = rt._compile_request(ep, initial).request
    accepted = LLMResponseIR(assistant("done"), LLMFinishReason.STOP,
        usage=LLMUsageIR(input_tokens=100_000, output_tokens=6_000, cached_input_tokens=72_000,
                         reported_fields=("input_tokens", "output_tokens")))
    rt._remember_usage(ep, initial, accepted)
    prompt = replace(initial, messages=(*initial.messages, accepted.message, user("continue")))
    budget = rt._budget_request(ep, prompt, verify=True)
    assert budget.budget_details["budget_source"] == "usage_delta"
    assert 106_000 < budget.estimated_input_tokens < 106_020
    assert transport.requests == []
    transport.count = 260_000
    near = replace(prompt, messages=(*prompt.messages, user("x" * 180_000)))
    verified = rt._budget_request(ep, near, verify=True)
    assert verified.estimated_input_tokens == transport.count
    assert verified.budget_details["baseline_tokens"] == 106_000
    assert [r.operation for r in transport.requests] == ["input_tokens"]
    payload = transport.requests[0].payload
    assert payload["tools"][0]["name"] == "read"
    assert all(text in str(payload["input"]) for text in ("history", "done", "continue", "x" * 180_000))
    for changed in (replace(prompt, messages=(user("edited"), *prompt.messages[1:])),
                    replace(prompt, tools=()), replace(prompt, logical_scope_id="another")):
        assert rt._budget_request(ep, changed, verify=True).budget_details["budget_source"] == "local"
    rt.release_context("resident")
    assert rt._budget_request(ep, prompt, verify=True).budget_details["baseline_tokens"] == 0


def test_native_compact_source_keeps_semantic_seed_and_omits_expired_controls():
    from tests.test_control_plane import PalControlFlowTests
    from pal.memory.contracts import L1MessageKind

    async def scenario():
        fixture = PalControlFlowTests()
        await fixture.asyncSetUp()
        rt, _, transport = runtime()
        fixture.core.context.port_registry["llm:llm"] = rt
        memory = fixture.memory_service
        memory.begin_l1_turn("old", user_text="old request")
        memory.append_l1_user("old", LLMMessageIR(MessageRole.USER, (TextPartIR("SEMANTIC_SEED_FACT"),),
            semantic_kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY))
        memory.prepare_l1_prompt_context("old", [], [{"key": "runtime:temporary", "role": "developer", "content": "EXPIRED_CONTROL"}], boundary="test")
        memory.upsert_l1_assistant("old", assistant())
        memory.settle_l1_turn("old")
        memory.history_root.promote()
        result = await fixture.core.turn_executor.compaction.run(memory,
            target_input_budget=8192, reserved_output_tokens=512, max_attempts=1)
        assert result.success, result.failures
        compact, = [r for r in transport.requests if r.operation == "compact"]
        assert "SEMANTIC_SEED_FACT" in str(compact.payload)
        assert "EXPIRED_CONTROL" not in str(compact.payload)
        fixture.core.close()

    asyncio.run(scenario())


def test_native_compact_preserves_completed_right_tail_when_only_the_tail_crosses_price():
    from tests.test_control_plane import PalControlFlowTests

    async def scenario():
        fixture = PalControlFlowTests()
        await fixture.asyncSetUp()
        rt, ep, transport = runtime()
        ep.context_window = 320_384
        fixture.core.context.port_registry["llm:llm"] = rt
        memory = fixture.memory_service
        for turn, text in (("old", "LEFT_268K"), ("recent", "RIGHT_8K")):
            memory.begin_l1_turn(turn, user_text=text)
            memory.upsert_l1_assistant(turn, assistant())
            memory.settle_l1_turn(turn)
            if turn == "old":
                memory.history_root.promote()

        original = transport.frames
        def frames(endpoint, req):
            sent = json.dumps(thaw_json(req.payload))
            if req.operation == "input_tokens":
                transport.count = ((268_000 if "LEFT_268K" in sent else 0)
                                   + (8_000 if "RIGHT_8K" in sent else 0) + 10)
            yield from original(endpoint, req)

        with patch.object(transport, "frames", frames):
            result = await fixture.core.turn_executor.compaction.run(memory,
                target_input_budget=272_000, reserved_output_tokens=512, max_attempts=2)
        assert result.success, result.failures
        paid = [r for r in transport.requests if r.operation == "compact"]
        assert len(paid) == 1
        assert "LEFT_268K" in str(paid[0].payload)
        assert "RIGHT_8K" not in str(paid[0].payload)
        assert any("RIGHT_8K" in m.text for m in memory.history_root.right_messages())

    asyncio.run(scenario())


def test_legacy_over_tier_native_source_gets_one_paid_attempt_on_failure():
    rt, ep, transport = runtime()
    transport.count = 280_000
    transport.error = True
    memory = source_memory()
    before = memory.history_root.all_turns(), memory.history_root.cut
    _, snap = snapshot(memory)
    result = asyncio.run(execute_compaction(CompactionEngine(PalCompactionPolicy(), max_attempts=3),
        snap, llm_runtime=rt, memory_service=memory))
    assert result.status == "failed" and result.attempts == 1
    assert sum(r.operation == "compact" for r in transport.requests) == 1
    assert (memory.history_root.all_turns(), memory.history_root.cut) == before


def test_gemini_count_payload_preserves_system_tools_and_declines_unknown_replay():
    from pal.llm.context_budget import count_payload
    wire = {"model": "gemini-3.1-pro-preview", "system_instruction": "system",
            "input": [{"type": "function_call", "id": "call", "name": "read", "arguments": {}, "signature": "sig"}],
            "tools": [{"type": "function", "name": "read", "description": "read", "parameters": {"type": "object"}}]}
    counted = count_payload(WireShape.GEMINI_INTERACTIONS, wire)["generateContentRequest"]
    assert counted["contents"][0]["parts"][0]["thoughtSignature"] == "sig"
    assert counted["systemInstruction"]["parts"] == [{"text": "system"}]
    assert counted["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == {"type": "object"}
    wire["input"].append({"type": "thought", "signature": "opaque"})
    assert count_payload(WireShape.GEMINI_INTERACTIONS, wire) is None


@pytest.mark.parametrize(("shape", "url", "model", "limit", "count_field"), [
    (WireShape.ANTHROPIC_MESSAGES, "https://api.anthropic.com", "claude-haiku-5-5", 100_000, "input_tokens"),
    (WireShape.GEMINI_INTERACTIONS, "https://generativelanguage.googleapis.com/v1beta",
     "gemini-3.1-pro-preview", 200_000, "totalTokens"),
])
def test_first_party_counts_effective_system_messages_and_tools(shape, url, model, limit, count_field):
    from pal.shared.tool_protocol import ToolDefinitionIR
    rt, ep, transport = runtime(model, base_url=url, wire_shape=shape, context_window=limit,
        provider="anthropic" if shape == WireShape.ANTHROPIC_MESSAGES else "google")
    prompt = replace(request(LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("fixed prompt"),)),
                             user("x" * limit * 4)), tools=(ToolDefinitionIR("read", "read", {"type": "object"}),))
    def frames(endpoint, req):
        assert req.operation == "input_tokens"
        body = str(req.payload)
        assert all(item in body for item in ("fixed prompt", "read", "xxxx"))
        yield _JSONFrame(0, {count_field: limit})

    with patch.object(transport, "frames", frames):
        result = rt._compile_request(ep, prompt)
    assert result.budget_details["budget_source"] == "provider"
    assert result.target_input_budget == limit
    assert not result.compact_required


def test_count_failure_keeps_large_semantic_request_under_normal_admission_rules():
    rt, ep, transport = runtime("gpt-6.2-sol", context_window=320_384)
    prompt = replace(request(user("x" * 1_088_008)), metadata={
        "purpose": "memory_compaction_engine", "compaction_policy": "pal"})
    with patch.object(transport, "frames", side_effect=TimeoutError("unavailable")):
        prepared = rt._compile_request(ep, prompt)
    assert prepared.compact_required
    assert prepared.budget_details["budget_source"] == "local_after_count_failure"
    assert "premium_compact" not in prepared.budget_details


def test_smaller_first_compact_source_ends_after_complete_pair():
    memory = source_memory()
    root = memory.history_root
    # Start from an empty cut, with a complete tool group followed by pending input.
    empty = MemoryService()
    for turn in root.all_turns():
        empty.history_root.store.append(turn)
    before = empty.history_root.all_turns()
    snap = empty.begin_left_compaction("first", reason="test", include_completed_tail=True)
    smaller = empty.history_root.restrict_compaction_to_left("first")
    assert 0 < len(smaller.message_ids()) < len(snap.message_ids())
    assert smaller.turns[-1].messages[-1].role == MessageRole.ASSISTANT
    empty.history_root.cancel("first")
    assert empty.history_root.all_turns() == before and not empty.history_root.left_messages()


def test_over_tier_semantic_handoff_does_not_repeat_a_paid_invalid_checkpoint():
    rt, _, transport = runtime("gpt-6.2-sol", context_window=320_384)
    transport.count = 280_000
    memory = source_memory()
    _, snap = snapshot(memory, native=False)
    snap = replace(snap, replay_request=request(user("x" * 1_088_008), assistant()))
    original = transport.frames

    def frames(endpoint, req):
        if req.operation != "generate":
            yield from original(endpoint, req)
            return
        transport.requests.append(req)
        yield _JSONFrame(0, {"id": "response", "status": "completed", "output": [{
            "type": "message", "id": "m", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "invalid checkpoint", "annotations": []}]}]})

    with patch.object(transport, "frames", frames):
        result = asyncio.run(execute_compaction(CompactionEngine(PalCompactionPolicy(), max_attempts=3),
            snap, llm_runtime=rt, memory_service=memory))
    assert result.status == "failed" and result.attempts == 1
    assert sum(r.operation == "generate" for r in transport.requests) == 1
