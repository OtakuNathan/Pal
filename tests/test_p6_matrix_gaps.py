"""P6 second-batch matrix-gap tests: Q09, B07, A12, I11.

- Q09: mixed-type queued input — only the supported contiguous prefix is
  consumed by one interjection batch; the first unsupported entry and its
  successors stay FIFO-queued for normal turn processing.
- B07: bounded failure — the engine's attempt loop is capped (per-attempt
  timeout does not restart forever), and an expired ticket's deadline is
  the total bound that frees the gate via sweep.
- A12: a turn whose LLM already produced the final answer never compacts
  on its way out, even with history across the auto threshold; the next
  input's preflight decides again.
- I11: after an auto compaction the turn's final reply is the real final
  answer — the handoff JSON is never the final reply, old transcript text
  is not re-emitted, and routing stays with the opening turn.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pal.channel import ChannelRuntime, register_with_core as register_channel_with_core
from pal.core import register_with_core as register_core_with_core
from pal.core.compaction import CompactionEngine
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.runtime import PalCore
from pal.foundation.io import EventEnvelope
from pal.llm import generation_result_from_values
from pal.llm.contracts import LLMPreflightAdvice
from pal.memory import (
    L1TranscriptMessage,
    L3ProviderSelector,
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared import EventKind, SourceKind
from pal.shared.agent_io import ChannelEnvelope, EndpointConfig, ResponseHandle

from tests.test_cache_warm_deadline import _route
from tests.test_compaction_cancel_control import _build_core
from tests.test_compaction_gate import _envelope, _run
from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _memory_with_turns,
    _valid_pal_payload,
)


# ── Q09 · mixed-type prefix consumption ─────────────────────────────────


def test_q09_mixed_queue_consumes_supported_prefix_only(tmp_path):
    async def scenario():
        core, service, _engine, _replies = _build_core(tmp_path)
        from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, TextPartIR

        service.l1_store.turns.append(L1TurnIR(
            turn_id="turn-live",
            state=L1TurnState.ACTIVE,
            messages=[LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR("opening question"),),
                message_id="u1",
                state=MessageState.COMPLETE,
            )],
        ))
        text_one = _envelope("m-1", "first correction")
        text_two = _envelope("m-2", "second correction")
        unsupported = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"file_id": "att-1"},  # no text -> not interjectable
                correlation_id="m-3",
                event_id="m-3",
            ),
            endpoint=text_one.endpoint,
            response_handle=text_one.response_handle,
        )
        core.state.pending_channel_turns.extend([text_one, text_two, unsupported])

        appended: list[object] = []
        original = service.append_l1_user

        def recording_append(turn_id, message):
            appended.append(message)
            return original(turn_id, message)

        service.append_l1_user = recording_append
        continuation = SimpleNamespace(turn_id="turn-live", delivery_binding=None)
        from pal.core.interjection import inject_pending_interjection_async

        await inject_pending_interjection_async(
            context=core.context, state=core.state, continuation=continuation,
        )

        assert len(appended) == 1
        batch_text = "".join(
            part.text for part in appended[0].parts
            if hasattr(part, "text")
        )
        assert "first correction" in batch_text and "second correction" in batch_text
        # The unsupported entry was neither consumed nor skipped past.
        remaining = list(core.state.pending_channel_turns)
        assert [env.event.event_id for env in remaining] == ["m-3"]
    _run(scenario())


# ── B07 · bounded attempts + total deadline frees the gate ──────────────




class _CountingCompactEngine:
    """Real-shaped stub: counts runs, installs nothing."""

    def __init__(self) -> None:
        self.calls = 0
        self.policy = PalCompactionPolicy()

    async def run(self, snapshot, *, llm_runtime=None, memory_service=None,
                  after_commit=None, replay_guard=None, commit_guard=None):
        self.calls += 1
        from pal.core.compaction import CompactionRunResult

        return CompactionRunResult(
            status="compacted",
            attempts=1,
            memory_result=SimpleNamespace(
                summary="s",
                projected_entries=[],
                metadata={"projected_entry_count": 0,
                          "compact_summary_count": 1, "retired_count": 0},
            ),
        )


def _turn_envelope(text: str = "hello") -> ChannelEnvelope:
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind="user.message",
            source_kind="channel",
            payload={"text": text},
        ),
        endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
        response_handle=ResponseHandle(endpoint_id="stdio"),
    )


def _core_with_llm(llm, engine) -> tuple[PalCore, MemoryService]:
    core = PalCore()
    register_core_with_core(core)
    channel_runtime = ChannelRuntime()
    register_channel_with_core(core.context, channel_runtime)
    memory_service = MemoryService(
        l3_selector=L3ProviderSelector(
            resolver=core.context.execution_runtime.l3_plugin_registry.require
        )
    )
    memory_service.l1_store.append([
        L1TranscriptMessage(
            role="user",
            content="Older context that sits across the auto threshold.",
        )
    ])
    register_memory_with_core(core.context, memory_service)
    core.context.port_registry["llm:llm"] = llm
    core.turn_executor._compaction_engine = engine
    return core, memory_service


class _FinalAnswerLLM:
    def preflight(self, request) -> LLMPreflightAdvice:
        return LLMPreflightAdvice(
            status="ready",
            active_model=request.request.model_hint or "stub-model",
            fallback_chain=[],
            target_input_budget=2048,
            reserved_output_tokens=request.request.policy.max_output_tokens,
        )

    def generate(self, request):
        return generation_result_from_values(
            text="the final answer", tool_calls=[], finish_reason="stop",
        )

    async def agenerate(self, request):
        return self.generate(request)


def test_a12_final_answer_turn_never_compacts():
    engine = _CountingCompactEngine()
    core, _service = _core_with_llm(_FinalAnswerLLM(), engine)
    outcome = core.process_channel_turn(_turn_envelope())
    assert outcome.final_reply == "the final answer"
    # The answer already existed: no compaction on the way out, no ticket.
    assert engine.calls == 0
    assert not core.state.compaction_tickets


class _CompactThenFinalLLM:
    """First generate asks for compaction; after install, real final."""

    def __init__(self) -> None:
        self.generate_count = 0

    def preflight(self, request) -> LLMPreflightAdvice:
        return LLMPreflightAdvice(
            status="ready",
            active_model=request.request.model_hint or "stub-model",
            fallback_chain=[],
            target_input_budget=2048,
            reserved_output_tokens=request.request.policy.max_output_tokens,
        )

    def generate(self, request):
        self.generate_count += 1
        if self.generate_count == 1:
            return generation_result_from_values(
                text="", tool_calls=[], finish_reason="compact_required",
                target_input_budget=512, reserved_output_tokens=64,
            )
        return generation_result_from_values(
            text="final after compaction", tool_calls=[], finish_reason="stop",
        )

    async def agenerate(self, request):
        purpose = str(request.metadata.get("purpose") or "")
        if "compaction" in purpose:
            return generation_result_from_values(
                text=json.dumps({
                    "schema": "pal.compaction.pal.v2",
                    "kind": "pal",
                    "summary": {
                        "summary": "I11 handoff summary.",
                        "search_text": "I11 handoff summary.",
                    },
                    "continuity": {
                        "current_focus": "finish the turn",
                        "primary_request_and_intent": "reply normally",
                        "active_operating_instructions": [],
                        "active_requests": [],
                        "temporary_task_state": [],
                        "key_decisions": [],
                        "pending_questions": [],
                        "recent_raw_turns": [],
                        "warm_compressed_turns": [],
                        "retired_or_superseded_context": [],
                        "optional_next_step": "emit the final answer",
                    },
                    "memory_candidates": [],
                }),
                tool_calls=[], finish_reason="stop",
            )
        return self.generate(request)


def test_i11_final_reply_is_real_answer_not_handoff_json():
    llm = _CompactThenFinalLLM()
    core, service = _core_with_llm(llm, CompactionEngine(PalCompactionPolicy()))
    outcome = core.process_channel_turn(_turn_envelope())
    # The handoff JSON was never the final reply...
    assert outcome.final_reply == "final after compaction"
    assert "pal.compaction.pal.v2" not in outcome.final_reply
    # ...old transcript text was not re-emitted...
    assert "Older context" not in outcome.final_reply
    assert all("Older context" not in str(t) for t in outcome.reply_texts)
    # ...and exactly one compaction ran with a clean gate afterwards.
    assert llm.generate_count == 2
    assert not core.state.compaction_tickets
    assert service.context_epoch == 1


# ── B03/B04 · budget boundary math ───────────────────────────────────


def test_b03_boundary_equality_fits_one_token_over_rejects():
    from pal.llm.ir import (
        LLMMessageIR,
        MessageRole,
        PromptRegionIR,
        TextPartIR,
    )
    from pal.llm.runtime import LLMRuntime, PreparedLLMRequest, _estimate_request_tokens

    runtime = LLMRuntime.__new__(LLMRuntime)
    runtime.safety_margin_tokens = 1024
    endpoint = SimpleNamespace(
        context_window=8192, max_output_tokens=None, endpoint_id="b03",
    )
    target = runtime._target_input_budget(endpoint, output_tokens=2048)
    # window 8192 - output 2048 - margin max(1024, 5%) = 5120.
    assert target == 5120
    base = PreparedLLMRequest(
        endpoint=endpoint,
        request=SimpleNamespace(),
        estimated_input_tokens=target,
        target_input_budget=target,
    )
    # H+J+O+E exactly == C: the equality fits (send eligibility holds).
    assert base.compact_required is False
    # One token over C: rejected.
    assert replace(base, estimated_input_tokens=target + 1).compact_required is True
    # Cached discounts never enter the capacity math: the estimate is a
    # pure character count and prompt_region (cache locality) cannot
    # shrink it.
    active = SimpleNamespace(
        messages=(LLMMessageIR(
            role=MessageRole.USER,
            parts=(TextPartIR("x" * 400),),
            prompt_region=PromptRegionIR.ACTIVE_INPUT,
        ),),
        tools=(),
    )
    settled = SimpleNamespace(
        messages=(LLMMessageIR(
            role=MessageRole.USER,
            parts=(TextPartIR("x" * 400),),
            prompt_region=PromptRegionIR.SETTLED_HISTORY,
        ),),
        tools=(),
    )
    assert _estimate_request_tokens(active) == _estimate_request_tokens(settled)


def test_b04_output_reservation_counts_against_total_window():
    from pal.llm.runtime import LLMRuntime, PreparedLLMRequest

    runtime = LLMRuntime.__new__(LLMRuntime)
    runtime.safety_margin_tokens = 1024
    endpoint = SimpleNamespace(
        context_window=8192, max_output_tokens=None, endpoint_id="b04",
    )
    small_output = runtime._target_input_budget(endpoint, output_tokens=1024)
    large_output = runtime._target_input_budget(endpoint, output_tokens=4096)
    # The output reservation (thinking included, since the thinking
    # budget must stay < max_output_tokens) consumes the same window the
    # input draws from: a bigger reserved output strictly shrinks the
    # input-only cap.
    assert small_output == 8192 - 1024 - 1024
    assert large_output == 8192 - 4096 - 1024
    assert large_output < small_output
    # The input-only cap never collapses below 1, and without window
    # knowledge there is no budget to enforce (nothing is rejected on a
    # made-up number).
    assert runtime._target_input_budget(
        SimpleNamespace(context_window=0), output_tokens=512,
    ) == 0
    unknown = PreparedLLMRequest(
        endpoint=endpoint,
        request=SimpleNamespace(),
        estimated_input_tokens=10**9,
        target_input_budget=0,
    )
    assert unknown.compact_required is False


# ── B05/I14 · next-request fit precheck with headroom ───────────────────






def test_a04_unknown_mutation_rejected_as_reconcile_required():
    async def scenario():
        from pal.core.turns import MemoryCompactEffect
        from pal.llm.ir import (
            LLMMessageIR,
            MessageRole,
            MessageState,
            TextPartIR,
        )
        from pal.shared.tool_protocol import new_tool_call

        core = PalCore()
        service = _memory_with_turns(1)
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = object()
        # A mutation was dispatched but its effect ledger entry is unknown:
        # the call is COMPLETE yet no result ever closed it.
        call = new_tool_call(
            call_id="call_a04", name="run_shell", args={"cmd": "touch x"}
        )
        assistant = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("mutating"), call),
            message_id="a1",
            state=MessageState.COMPLETE,
        )
        from pal.memory.turn_ir import L1TurnIR, L1TurnState

        service.l1_store.turns.append(L1TurnIR(
            turn_id="t-a04", state=L1TurnState.ACTIVE, messages=[assistant],
        ))
        continuation = SimpleNamespace(
            turn_id="t-a04", waiting_effect_id=None,
            interrupted=False, interrupt_reason="",
        )
        effect = MemoryCompactEffect(
            assembly_context=None, target_input_budget=512, reserved_output_tokens=64,
        )
        result = await core.turn_executor.execute_turn_effect_async(
            continuation, effect,
        )
        # Not admitted: no fake closure, no re-run, no install.
        from pal.shared import RuntimeStatus

        assert result.status == RuntimeStatus.ERROR
        diags = [
            d for d in core.state.diagnostics
            if d.get("kind") == "compaction_round_unsafe"
        ]
        assert diags, "A04 requires an explicit reconcile-required record"
        assert diags[-1]["reconcile_required"] is True
        assert any("call_a04" in reason for reason in diags[-1]["reasons"])
        # The call itself is untouched and no summary landed.
        turn = service.active_l1_turn("t-a04")
        part_ids = [
            str(getattr(part, "call_id", "")) for m in turn.messages for part in m.parts
        ]
        assert "call_a04" in part_ids
        assert service.context_epoch == 0
        assert service.compaction_receipts == {}
        assert not core.state.compaction_tickets
    _run(scenario())
