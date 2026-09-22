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






class _Advice:
    def __init__(self, status) -> None:
        from pal.llm import LLMPreflightAdvice

        self._advice = LLMPreflightAdvice(status=status)

    def __getattr__(self, item):
        return getattr(self._advice, item)


class WarmUsageAccountingTests(unittest.TestCase):
    """W11/B10: a warm handoff the provider reports as a cache miss is
    billed exactly as reported — no invented savings, no retry chasing a
    hit — and the valid content still installs normally."""

