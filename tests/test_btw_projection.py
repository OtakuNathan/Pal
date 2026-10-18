from dataclasses import replace
import hashlib
import json

import pytest

from pal.core import PalCore  # initialize the shared runtime imports
from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR, WireShape
from pal.llm.projection_contracts import AppendReceipt, AttemptKey, HistoryCommitReceipt, HistoryCursor, LogicalSessionId, OwnerFence
from pal.llm.projection_session import EndpointProjectionSession, HistoryView
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from tests.test_projection_session import _binding
from tests.test_projection_retirement_ownership import _merged_session
from tests.test_prompt_cache_evidence import _request
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.shared.tool_protocol import ToolResultIR, new_tool_call


def message(role, text, mid):
    return LLMMessageIR(role=role, parts=(TextPartIR(text),), message_id=mid)


@pytest.mark.parametrize("shape", list(WireShape))
def test_fork_replays_only_closed_left_without_touching_active_parent(shape):
    if shape.value not in {"openai_completion", "openai_response", "anthropic_messages"}:
        pytest.skip("shape has no session projection")
    source = EndpointProjectionSession(LogicalSessionId("pal:resident"))
    binding = _binding(shape)
    source.bind(binding)
    left = (message(MessageRole.USER, "left question", "l-u"), message(MessageRole.ASSISTANT, "left answer", "l-a"))
    right = (message(MessageRole.USER, "right question", "r-u"), message(MessageRole.ASSISTANT, "right answer", "r-a"))
    shell = LLMRequestIR(messages=(message(MessageRole.SYSTEM, "unchanged system", "sys"),), tools=(),
                         logical_scope_id="pal:resident", policy=GenerationPolicyIR(max_output_tokens=128))
    for index, pair in enumerate((left, right)):
        attempt = AttemptKey(source.identity, OwnerFence(0), f"main-{index}")
        source.begin_round(attempt, requires_native=False)
        before = source.frontier
        source.prepare_normal(HistoryView(cursor=before, messages=(pair[0],)), request_shell=shell)
        source.observe_commit(HistoryCommitReceipt(attempt=attempt,
            append=AppendReceipt(before, HistoryCursor(0, index + 1, hashlib.sha256(str(index).encode()).hexdigest()), 1),
            closed_call_ids=(), native_committed=False),
            accepted_messages=(pair[1],), span_message_ids=tuple(m.message_id for m in pair))
    source.begin_round(AttemptKey(source.identity, OwnerFence(0), "in-flight-main"), requires_native=False)
    before = (source.frontier, source.chunks, source.active_attempt, source.covered_message_ids())
    branch = source.fork_prefix(LogicalSessionId("btw:one"), tuple(m.message_id for m in left))
    assert (source.frontier, source.chunks, source.active_attempt, source.covered_message_ids()) == before
    assert set(branch.covered_message_ids()) == {"l-u", "l-a"}
    source.retire()
    question = message(MessageRole.USER, "side question", "btw-u")
    branch.begin_round(AttemptKey(branch.identity, OwnerFence(0), "btw-attempt"), requires_native=False)
    prepared = branch.prepare_normal(HistoryView(cursor=branch.frontier, messages=(question,)), request_shell=shell)
    expected = codec_for_shape(shape).encode(replace(shell, messages=(*shell.messages, *left, question)),
        ShapeContext(wire_shape=shape, endpoint_id=binding.endpoint_id, model_id=binding.model_id))
    assert json.loads(prepared.payload_json) == expected.payload
    assert "right answer" not in prepared.payload_json
    # A tool round needs a further request in this same one-shot turn. The
    # captured L is already frozen and must not be appended by the first commit.
    answer = LLMMessageIR(role=MessageRole.ASSISTANT, message_id="btw-a", parts=(
        new_tool_call(name="read_fixture", args={}, call_id="side-read"),))
    branch.observe_commit(HistoryCommitReceipt(
        attempt=branch.active_attempt,
        append=AppendReceipt(branch.frontier, HistoryCursor(0, 1, "f" * 64), 1),
        closed_call_ids=(), native_committed=False),
        accepted_messages=(answer,), span_message_ids=(question.message_id, answer.message_id))
    branch.begin_round(AttemptKey(branch.identity, OwnerFence(0), "btw-followup"), requires_native=False)
    followup = LLMMessageIR(role=MessageRole.TOOL, message_id="btw-tool-result", parts=(
        ToolResultIR(call_id="side-read", name="read_fixture", content="read-only fixture content"),))
    continued = branch.prepare_normal(HistoryView(cursor=branch.frontier, messages=(followup,)), request_shell=shell)
    expected = codec_for_shape(shape).encode(replace(shell, messages=(*shell.messages, *left, question, answer, followup)),
        ShapeContext(wire_shape=shape, endpoint_id=binding.endpoint_id, model_id=binding.model_id))
    assert json.loads(continued.payload_json) == expected.payload


def test_anthropic_fork_trims_merged_right_blocks():
    source, _ = _merged_session()
    branch = source.fork_prefix(LogicalSessionId("btw:merged"), ("old-seed", "old-user"))
    branch.begin_round(AttemptKey(branch.identity, OwnerFence(0), "side"), requires_native=False)
    question = message(MessageRole.USER, "SIDE_QUESTION", "btw-u")
    prepared = branch.prepare_normal(HistoryView(cursor=branch.frontier, messages=(question,)))
    assert "RETIRED_SEED" in prepared.payload_json
    assert "RETIRED_INSTRUCTION" in prepared.payload_json
    assert "KEEP_QUESTION" not in prepared.payload_json
    assert "KEEP_ANSWER" not in prepared.payload_json
    assert "SIDE_QUESTION" in prepared.payload_json
    assert "right-q" in source.covered_message_ids()


def test_side_cache_key_is_shared_but_does_not_take_over_warm_anchor():
    request = replace(_request(), metadata={"turn_id": "main"})
    context = ShapeContext(wire_shape=WireShape.OPENAI_RESPONSE, endpoint_id="e", model_id="m")
    cache = PromptCacheCoordinator()
    main = cache.plan(request, context)
    main_evidence = dict(cache._anchor_evidence)
    side = replace(request, metadata={"turn_id": "btw", "transient_history": True})
    plan = cache.plan(side, context)
    assert plan.cache_key == main.cache_key
    assert plan.scope_key != main.scope_key
    assert plan.reminder is None
    assert cache._last_plan is main
    assert cache._anchor_evidence == main_evidence


@pytest.mark.parametrize("shape", ["openai_completion", "openai_response", "anthropic_messages"])
def test_real_model_runtime_uses_branch_projection_and_isolates_receipts(shape):
    import asyncio
    from types import SimpleNamespace

    from pal.core.btw import start_btw
    from pal.llm import EndpointResolver, LLMRuntime
    from pal.llm.endpoint import ShapeEndpointInvoker
    from pal.llm.projection_session import LeftReplacement
    from tests.test_btw import action, make_core, seed_left
    from tests.test_v3_n3_invoker_projection import CapturingTransport, endpoint
    from tests.test_prompt_cache_v2_runtime import Settings

    async def scenario():
        ep = endpoint()
        ep.wire_shape = shape
        ep.supports_streaming = False
        payload = {
            "openai_completion": {"choices": [{"message": {"role": "assistant", "content": "side answer"}, "finish_reason": "stop"}]},
            "openai_response": {"id": "resp-side", "status": "completed", "output": [
                {"id": "msg-side", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "side answer"}]}]},
            "anthropic_messages": {"id": "msg-side", "type": "message", "role": "assistant", "content": [
                {"type": "text", "text": "side answer"}], "stop_reason": "end_turn"},
        }[shape]
        transport = CapturingTransport([payload])
        class SelectedSettings(Settings):
            def get_active_llm_endpoint_id(self):
                return ep.endpoint_id
        model = LLMRuntime(endpoint_resolver=EndpointResolver(endpoints=(ep,)), settings_repository=SelectedSettings(),
            endpoint_invoker=ShapeEndpointInvoker(transport=transport),
            config=SimpleNamespace(llm_endpoint_retry_attempts=1))
        core, events = make_core(model)
        memory = core.context.require_port("memory:memory")
        left = seed_left(memory)
        source = model.endpoint_projection_session("pal:resident")
        source.on_left_replaced(LeftReplacement(seed_messages=left.messages,
            seed_coverage_ids=tuple(m.message_id for m in left.messages), kept_frozen_messages=(),
            cursor_after=HistoryCursor(1, 1, "d" * 64), left_revision=1))
        active = AttemptKey(source.identity, OwnerFence(0), "main-still-running")
        source.begin_round(active, requires_native=False)
        sentinel = object()
        model.last_projection_receipt = sentinel
        before = (source.frontier, source.chunks, source.active_attempt)
        await start_btw(core, action())
        branch = core._btw_turn
        await branch.task
        assert events[-1][1]["status"] == "success", core.state.diagnostics
        assert transport.captured is not None, core.state.diagnostics
        assert "old answer" in json.dumps(transport.captured.payload)
        assert model.last_projection_receipt is sentinel
        assert branch.llm.endpoint_invoker.prompt_cache is not model.endpoint_invoker.prompt_cache
        assert branch.llm.usage_ledger is model.usage_ledger
        assert branch.llm.endpoint_invoker.transport is model.endpoint_invoker.transport
        assert branch.llm.last_projection_receipt.applied
        assert (source.frontier, source.chunks, source.active_attempt) == before
        assert memory.history.turns == (left,)
        core.close()
    asyncio.run(scenario())
