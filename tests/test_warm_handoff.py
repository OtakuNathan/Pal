"""P3 warm handoff acceptance tests (TEST_MATRIX W-class, P3 items).

The frozen-replay construction is the invariance proof: the anchor request
(after-hooks envelope) is carried verbatim; only the accepted suffix and one
tail instruction are appended. Eligibility refusals fall back to cold on the
same source stamp/coverage (W21).

Mapped here: W01/W02/W03 (prefix+suffix+coverage), W04/W05/W06/W07/W17/W20
(invariance by construction), W08/W09 (binding and purpose), W12/W14/W13/
W15/W16/W18 (refusals and no-side-effect), W21 (fallback), W22 (retry
prefix), W23 (no eager cache-write). W10/W11 remain covered by the hot-cache
suite; W19 by the P1 round-safe gate.
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from pal.core.compaction import CompactionEngine, CompactionSnapshot, CompactionClockKind
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.prompt_context import CONTEXT_KIND
from pal.core.runtime import PalCore
from pal.llm import generation_result_from_values
from pal.llm.ir import (
    GenerationPolicyIR,
    ImagePartIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    PromptRegionIR,
    TextPartIR,
)
from pal.memory.contracts import L1MessageKind, MemoryPackRequest
from pal.memory.turn_ir import L1TurnState
from pal.shared import LLMFinishReason, LLMPreflightStatus
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _memory_with_turns,
    _valid_pal_payload,
)


SYSTEM_TEXT = "SYSTEM_HOOK_MARKER once only"
TOOL_A = {"name": "local_query", "description": "local tool", "execute_on": "client"}


def _warm_service(*, rounds_after_anchor: int = 0, active: bool = False):
    """L1: [anchor-turn] + N settled rounds + optional active turn."""
    from pal.memory import MemoryService

    service = MemoryService()
    service.begin_l1_turn("anchor-turn", user_text="ANCHOR_REQUEST")
    service.upsert_l1_assistant(
        "anchor-turn",
        LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR("ANCHOR_REPLY"),)),
    )
    service.settle_l1_turn("anchor-turn")
    anchor_id = service.l1_store.turns.get("anchor-turn").messages[0].message_id
    for index in range(rounds_after_anchor):
        turn_id = f"settled-{index}"
        service.begin_l1_turn(turn_id, user_text=f"LATER_REQUEST_{index}")
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR(f"LATER_REPLY_{index}"),)),
        )
        service.settle_l1_turn(turn_id)
    active_id = ""
    if active:
        active_id = "active-cut"
        service.begin_l1_turn(active_id, user_text="ACTIVE_QUESTION")
        service.upsert_l1_assistant(
            active_id,
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    TextPartIR("ACTIVE_DECISION"),
                    new_tool_call(call_id="call-W", name="read_file", arguments={"file": "w.py"}),
                ),
            ),
        )
        service.append_l1_tool_result(
            active_id,
            ToolResultIR(call_id="call-W", name="read_file", content="ACTIVE_RESULT"),
        )
    return service, anchor_id, active_id


def _anchor_llm(
    anchor_id: str,
    *,
    request: LLMRequestIR | None = None,
    dialect: str = "openrouter_openai_explicit",
    wire_shape: str = "openai_response",
    outcomes: list | None = None,
):
    llm = _ScriptedLLM(list(outcomes or []))
    base = request or LLMRequestIR(
        messages=(
            LLMMessageIR(
                role=MessageRole.SYSTEM,
                parts=(TextPartIR(SYSTEM_TEXT),),
                message_id="system-1",
            ),
            LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR("ANCHOR_REQUEST"),),
                message_id=anchor_id,
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
        ),
        tools=(dict(TOOL_A),),
        policy=GenerationPolicyIR(max_output_tokens=1024),
        model_hint="gpt-5.6-luna",
        logical_scope_id="pal:resident",
        metadata={"preferred_endpoint_id": "anchor-endpoint"},
    )
    llm.prompt_cache_confirmed_anchor_request = lambda **kwargs: {
        "request": base,
        "anchor_message_id": anchor_id,
        "dialect": dialect,
        "wire_shape": wire_shape,
    }
    llm.base_anchor = base
    return llm


def _build_replay(service, llm, executor=None, **kwargs):
    core = PalCore() if executor is None else None
    ex = executor or (core.turn_executor)
    return ex._resident_compaction_replay_request(
        service,
        llm_runtime=llm,
        logical_scope_id="pal:resident",
        preferred_endpoint_id=kwargs.pop("preferred_endpoint_id", None),
        preferred_model_id=kwargs.pop("preferred_model_id", None),
        include_active=kwargs.pop("include_active", False),
        active_turn_id=kwargs.pop("active_turn_id", ""),
    )


def _capture_warm(service, llm, *, continuation=None):
    """compact_memory_async-shaped snapshot with warm replay attached."""
    core = PalCore()
    core.context.port_registry["memory:memory"] = service
    core.context.port_registry["llm:llm"] = llm
    snapshot = CompactionSnapshot.capture(
        service,
        target_input_budget=8_192,
        reserved_output_tokens=2_048,
        clock_kind=CompactionClockKind.USER_TURN,
        clock_value=2,
        metadata={"compaction_op_id": "op-warm"},
        source_epoch=service.context_epoch,
    )
    replay, dialect, wire = core.turn_executor._resident_compaction_replay_request(
        service,
        llm_runtime=llm,
        logical_scope_id="pal:resident",
        preferred_endpoint_id=None,
        include_active=continuation is not None,
        active_turn_id=str(getattr(continuation, "turn_id", "") or ""),
    )
    return replace(snapshot, replay_request=replay, replay_dialect=dialect, replay_wire_shape=wire)


class WarmBuilderTests(unittest.TestCase):
    def test_w01_frozen_prefix_plus_suffix_tools_system_intact(self) -> None:
        service, anchor_id, _ = _warm_service(rounds_after_anchor=1)
        llm = _anchor_llm(anchor_id)
        replay, dialect, wire = _build_replay(service, llm)
        self.assertIsNotNone(replay)
        self.assertEqual((dialect, wire), ("openrouter_openai_explicit", "openai_response"))
        anchor = llm.base_anchor
        self.assertEqual(replay.messages[: len(anchor.messages)], anchor.messages)
        self.assertEqual(replay.tools, anchor.tools)
        self.assertEqual(replay.policy, anchor.policy)
        suffix_texts = [m.text for m in replay.messages[len(anchor.messages):]]
        self.assertIn("LATER_REQUEST_0", " ".join(suffix_texts))
        self.assertIn("LATER_REPLY_0", " ".join(suffix_texts))

    def test_w02_anchor_behind_two_rounds_suffix_carries_both(self) -> None:
        service, anchor_id, _ = _warm_service(rounds_after_anchor=2)
        llm = _anchor_llm(anchor_id)
        replay, _, _ = _build_replay(service, llm)
        self.assertIsNotNone(replay)
        suffix_text = " ".join(m.text for m in replay.messages[len(llm.base_anchor.messages):])
        for sentinel in ("LATER_REQUEST_0", "LATER_REPLY_0", "LATER_REQUEST_1", "LATER_REPLY_1"):
            self.assertIn(sentinel, suffix_text)

    def test_w03_active_cut_covered_exactly_once_in_auto_shape(self) -> None:
        service, anchor_id, active_id = _warm_service(rounds_after_anchor=1, active=True)
        llm = _anchor_llm(anchor_id)
        replay, _, _ = _build_replay(
            service, llm, include_active=True, active_turn_id=active_id
        )
        self.assertIsNotNone(replay)
        suffix = replay.messages[len(llm.base_anchor.messages):]
        joined = " ".join(m.text or "" for m in suffix)
        self.assertIn("ACTIVE_QUESTION", joined)
        self.assertIn("ACTIVE_DECISION", joined)
        self.assertIn("ACTIVE_RESULT", joined)
        # Idle shape must not smuggle the active cut into a manual compaction.
        idle_replay, _, _ = _build_replay(service, llm)
        if idle_replay is not None:
            idle_text = " ".join(
                m.text or "" for m in idle_replay.messages[len(llm.base_anchor.messages):]
            )
            self.assertNotIn("ACTIVE_RESULT", idle_text)

    def test_w09_cross_endpoint_or_model_binding_refused(self) -> None:
        service, anchor_id, _ = _warm_service()
        llm = _anchor_llm(anchor_id)
        replay, _, _ = _build_replay(
            service, llm, preferred_endpoint_id="other-endpoint"
        )
        self.assertIsNone(replay)
        replay, _, _ = _build_replay(service, llm, preferred_model_id="other-model")
        self.assertIsNone(replay)

    def test_w13_provider_hosted_tool_refuses_warm(self) -> None:
        service, anchor_id, _ = _warm_service()
        hosted = {"name": "web_search", "description": "server side", "execute_on": "server"}
        llm = _anchor_llm(
            anchor_id, request=replace(_anchor_llm(anchor_id).base_anchor, tools=(hosted,))
        )
        replay, _, _ = _build_replay(service, llm)
        self.assertIsNone(replay)

    def test_w14_forced_tool_choice_refuses_warm(self) -> None:
        service, anchor_id, _ = _warm_service()
        forced = replace(
            _anchor_llm(anchor_id).base_anchor,
            policy=GenerationPolicyIR(max_output_tokens=1024, tool_choice="required"),
        )
        llm = _anchor_llm(anchor_id, request=forced)
        replay, _, _ = _build_replay(service, llm)
        self.assertIsNone(replay)

    def test_w16_unknown_wire_shape_refuses_warm(self) -> None:
        service, anchor_id, _ = _warm_service()
        llm = _anchor_llm(anchor_id, dialect="", wire_shape="")
        replay, _, _ = _build_replay(service, llm)
        self.assertIsNone(replay)

    def test_w18_image_in_anchor_prefix_refuses_warm(self) -> None:
        service, anchor_id, _ = _warm_service()
        base = _anchor_llm(anchor_id).base_anchor
        with_image = replace(
            base,
            messages=(
                base.messages[0],
                LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(ImagePartIR(source="https://cdn.example/expiring.png"),),
                    message_id="img-1",
                ),
                base.messages[1],
            ),
        )
        llm = _anchor_llm(anchor_id, request=with_image)
        replay, _, _ = _build_replay(service, llm)
        self.assertIsNone(replay)


class WarmEngineTests(unittest.TestCase):
    def _run(self, snapshot, llm, service):
        return asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                snapshot, llm_runtime=llm, memory_service=service
            )
        )

    def test_w05_w06_w07_w22_w23_tail_retry_and_invariance(self) -> None:
        service, anchor_id, _ = _warm_service(rounds_after_anchor=1)
        llm = _anchor_llm(
            anchor_id,
            outcomes=[
                generation_result_from_values(text="INVALID not json"),
                generation_result_from_values(text=_valid_pal_payload("warm seed")),
            ],
        )
        snapshot = _capture_warm(service, llm)
        self.assertIsNotNone(snapshot.replay_request)
        result = self._run(snapshot, llm, service)
        self.assertTrue(result.success, result.failures)
        self.assertEqual(result.attempts, 2)
        first, second = llm.generate_requests[0], llm.generate_requests[1]
        # W22: retries share the identical stable prefix. The repair pass
        # may append the failed attempt-local output and a corrected tail
        # after it; the frozen prefix itself never changes.
        prefix_length = len(llm.base_anchor.messages) + (
            len(first.messages) - len(llm.base_anchor.messages) - 1
        )
        self.assertEqual(
            second.messages[:prefix_length], first.messages[:prefix_length]
        )
        # W05/W01: the frozen prefix is byte-equal to the anchor envelope.
        self.assertEqual(
            first.messages[: len(llm.base_anchor.messages)],
            llm.base_anchor.messages,
        )
        self.assertEqual(first.tools, llm.base_anchor.tools)
        # W06: the handoff instruction rides in ONE trailing message.
        tail = first.messages[-1]
        self.assertIn("Request Constraint", tail.text)
        self.assertNotIn("Request Constraint", first.messages[-2].text)
        # W07: the hook marker appears exactly once (frozen system only).
        system_texts = [
            m.text for m in first.messages if m.role == MessageRole.SYSTEM
        ]
        self.assertEqual(sum(text.count("SYSTEM_HOOK_MARKER") for text in system_texts), 1)
        # W23: no eager cache-write markers were added by the compactor.
        for key in first.metadata:
            self.assertNotIn("cache_write", key)
            self.assertNotIn("cache_control", key)
        self.assertEqual(first.metadata.get("purpose"), "memory_compaction_engine")
        self.assertTrue(first.metadata.get("model_hooks_already_applied"))
        # The install is a normal full-source compaction (W08 attribution:
        # purpose-tagged request; result lands only via the compact path).
        self.assertEqual(service.context_epoch, 1)
        self.assertIn("op-warm", service.compaction_receipts)

    def test_w12_tool_call_response_is_never_dispatched(self) -> None:
        from pal.llm.ir import LLMResponseIR

        service, anchor_id, _ = _warm_service()
        llm = _anchor_llm(anchor_id)
        tool_call_response = generation_result_from_values(text="")
        tool_call_response = replace(
            tool_call_response,
            response=replace(
                tool_call_response.response,
                message=replace(
                    tool_call_response.response.message,
                    parts=(
                        TextPartIR(""),
                        new_tool_call(call_id="w12", name="local_query", arguments={}),
                    ),
                ),
            ),
        )
        llm = _anchor_llm(
            anchor_id,
            outcomes=[tool_call_response, generation_result_from_values(text=_valid_pal_payload())],
        )
        snapshot = _capture_warm(service, llm)
        result = self._run(snapshot, llm, service)
        # A tool-call answer fails schema validation and is retried as text;
        # nothing dispatches it (there is no tool executor in this path).
        self.assertTrue(result.success, result.failures)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(service.context_epoch, 1)

    def test_w15_expired_instruction_drops_replay_to_scope_safe_cold(self) -> None:
        service, anchor_id, _ = _warm_service()
        base = _anchor_llm(anchor_id).base_anchor
        with_expired = replace(
            base,
            messages=(
                base.messages[0],
                LLMMessageIR(
                    role=MessageRole.DEVELOPER,
                    parts=(TextPartIR("ONE_SHOT_EXPIRED_DIRECTIVE"),),
                    semantic_kind=CONTEXT_KIND,
                    metadata={"pal_authored": True, "context_key": "expired"},
                    message_id="expired-1",
                ),
                base.messages[1],
            ),
        )
        llm = _anchor_llm(
            anchor_id,
            request=with_expired,
            outcomes=[generation_result_from_values(text=_valid_pal_payload("cold fallback"))],
        )
        snapshot = _capture_warm(service, llm)
        self.assertIsNotNone(snapshot.replay_request)
        result = self._run(snapshot, llm, service)
        self.assertTrue(result.success, result.failures)
        source_seen = llm.generate_requests[0].messages[-1].text
        self.assertNotIn("ONE_SHOT_EXPIRED_DIRECTIVE", source_seen)
        self.assertIn("ANCHOR_REQUEST", source_seen)

    def test_w21_replay_budget_drop_falls_back_same_stamp(self) -> None:
        service, anchor_id, _ = _warm_service()
        llm = _anchor_llm(
            anchor_id,
            outcomes=[generation_result_from_values(text=_valid_pal_payload("cold same stamp"))],
        )
        llm.preflight_hook = lambda request: _Advice(
            LLMPreflightStatus.COMPACT_REQUIRED
            if request.request.metadata.get("preferred_endpoint_source") == "prompt_cache_replay"
            else LLMPreflightStatus.READY
        )
        snapshot = _capture_warm(service, llm)
        self.assertIsNotNone(snapshot.replay_request)
        result = self._run(snapshot, llm, service)
        self.assertTrue(result.success, result.failures)
        receipt = service.compaction_receipts["op-warm"]
        self.assertEqual(receipt.source_stamp, snapshot.source_stamp)
        # The accepted request was the cold one, on the same source.
        sent = llm.generate_requests[0]
        self.assertIsNone(sent.metadata.get("preferred_endpoint_source"))


class _Advice:
    def __init__(self, status) -> None:
        from pal.llm import LLMPreflightAdvice

        self._advice = LLMPreflightAdvice(status=status)

    def __getattr__(self, item):
        return getattr(self._advice, item)


if __name__ == "__main__":
    unittest.main()
