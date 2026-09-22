"""Red-first tests for the two-segment WARM handoff split (主项1).

The v3 compact path must stop being cold-left-only: when a provider-
confirmed cached anchor exists AND the anchor prefix lies entirely inside
the LEFT segment, the compact LLM request is the anchor request verbatim
(byte-stable cached prefix) + the accepted LEFT suffix after the anchor +
the compaction instruction tail — the RIGHT side never enters the summary
source (I10).  Ineligible shapes honestly fall back to the cold-left
source builder (W21).
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from pal.core.turns import LLMRequestEffect
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR,
)
from pal.shared import PromptAssemblyContext
from tests.test_runtime_compaction import _valid_pal_payload
from tests.test_v3_n1_root_lifecycle import assistant, user
from tests.test_v3_n3_vertical_trace import (
    CapturingTransport, _drive, _executor, _request_for, _runtime,
)


def _anchor_request(memory_ids: dict, *, system_text: str = "ANCHOR-BASE") -> LLMRequestIR:
    return LLMRequestIR(
        messages=(
            LLMMessageIR(role=MessageRole.SYSTEM,
                         parts=(TextPartIR(system_text),), message_id="anchor-system"),
            LLMMessageIR(role=MessageRole.USER,
                         parts=(TextPartIR("Q1"),), message_id=memory_ids["q1"]),
            LLMMessageIR(role=MessageRole.ASSISTANT,
                         parts=(TextPartIR("A1 answer"),), message_id=memory_ids["a1"]),
        ),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=1024),
        model_hint="trace-model",
        logical_scope_id="pal:resident",
        metadata={"preferred_endpoint_id": "trace-endpoint"},
    )


def _stub_anchor_reader(anchor: LLMRequestIR, anchor_id: str):
    """Anchor material for BOTH readers: eligible drives ordinary
    autocompact (R3/S1, review 95373ef); confirmed stays the
    diagnostics/hot-only surface."""
    return lambda **kwargs: {
        "request": anchor,
        "anchor_message_id": anchor_id,
        "dialect": "openrouter_openai_explicit",
        "wire_shape": "openai_completion",
    }


def _memory_with_left_and_right():
    """One active turn: closed group (q1, a1) promoted into L; q2 stays in R."""
    from pal.memory import MemoryService

    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
    memory.stream_l1_assistant("T", assistant("A1 answer", "a1"))
    memory.append_l1_user("T", user("Q2", "q2"))
    memory.history_root.promote(include_active=True)
    ids = {
        "q1": memory.l1_store.turns.get("T").messages[0].message_id,
    }
    # Resolve ids by content order for robustness.
    messages = memory.l1_store.turns.get("T").messages
    ids["q1"] = messages[0].message_id
    ids["a1"] = messages[1].message_id
    ids["q2"] = messages[2].message_id
    left_ids = [m.message_id for m in memory.history_root.left_messages()]
    right_ids = [m.message_id for m in memory.history_root.right_messages()]
    assert ids["q1"] in left_ids and ids["a1"] in left_ids, (left_ids, right_ids)
    assert ids["q1"] not in right_ids
    return memory, ids


def _compact(ex, memory, *, continuation_turn="T"):
    return asyncio.run(ex.compact_memory_async(
        memory, target_input_budget=100_000, reserved_output_tokens=1024,
        continuation=SimpleNamespace(turn_id=continuation_turn),
    ))


class TwoSegmentWarmSplitTests(unittest.TestCase):
    def test_compact_uses_warm_replay_when_anchor_inside_left(self):
        memory, ids = _memory_with_left_and_right()
        transport = CapturingTransport([_valid_pal_payload("SUMMARY SEED")])
        runtime = _runtime(transport)
        anchor = _anchor_request(ids)
        runtime.prompt_cache_eligible_anchor_request = runtime.prompt_cache_confirmed_anchor_request = _stub_anchor_reader(
            anchor, ids["a1"])
        ex = _executor(memory, runtime)

        result = _compact(ex, memory)
        self.assertTrue(result.success, getattr(result, "failures", None))
        self.assertEqual(result.status, "compacted")

        self.assertEqual(len(transport.captured), 1,
                         "exactly one compact generation request")
        payload = json.dumps(dict(transport.captured[0].payload))
        messages = list(transport.captured[0].payload.get("messages") or [])
        # Warm: the anchor's own system text rides verbatim as the base.
        self.assertTrue(
            messages and "ANCHOR-BASE" in json.dumps(messages[0]),
            "the cached anchor prefix must be the request base, not a cold "
            "re-encode")
        # The replay tail instruction is present.
        self.assertIn("Source Semantics", payload)
        # I10: the RIGHT side never enters the summary source.
        self.assertNotIn("Q2", payload,
                         "right-side content must stay out of the compact "
                         "request (warm replay is left-bounded)")
        # The install still won and the seed landed.
        root = memory.history_root
        self.assertIn("SUMMARY SEED", "".join(
            getattr(part, "text", "")
            for message in root.left_messages()
            for part in message.parts
        ) or "SUMMARY SEED" in json.dumps([
            [getattr(p, "text", "") for p in m.parts]
            for m in root.left_messages()
        ]))

    def test_compact_falls_back_cold_when_anchor_missing(self):
        memory, ids = _memory_with_left_and_right()
        transport = CapturingTransport([_valid_pal_payload("COLD SEED")])
        runtime = _runtime(transport)
        runtime.prompt_cache_eligible_anchor_request = runtime.prompt_cache_confirmed_anchor_request = lambda **kwargs: {}
        ex = _executor(memory, runtime)

        result = _compact(ex, memory)
        self.assertTrue(result.success, getattr(result, "failures", None))

        self.assertEqual(len(transport.captured), 1)
        payload = json.dumps(dict(transport.captured[0].payload))
        self.assertNotIn("ANCHOR-BASE", payload,
                         "no anchor → the honest cold-left source builder")
        # Cold source renders the LEFT transcript content.
        self.assertIn("Q1", payload)
        self.assertIn("A1", payload)
        self.assertNotIn("Q2", payload,
                         "cold-left also keeps the right side out (I10)")

    def test_compact_falls_back_cold_when_anchor_beyond_left(self):
        """Anchor prefix reaching into R (or beyond the cut) refuses warm."""
        memory, ids = _memory_with_left_and_right()
        transport = CapturingTransport([_valid_pal_payload("COLD SEED")])
        runtime = _runtime(transport)
        # Anchor request carries q2 (right side) inside its cached prefix.
        anchor = LLMRequestIR(
            messages=(
                LLMMessageIR(role=MessageRole.SYSTEM,
                             parts=(TextPartIR("ANCHOR-BASE"),),
                             message_id="anchor-system"),
                LLMMessageIR(role=MessageRole.USER,
                             parts=(TextPartIR("Q1"),), message_id=ids["q1"]),
                LLMMessageIR(role=MessageRole.ASSISTANT,
                             parts=(TextPartIR("A1 answer"),),
                             message_id=ids["a1"]),
                LLMMessageIR(role=MessageRole.USER,
                             parts=(TextPartIR("Q2"),), message_id=ids["q2"]),
            ),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=1024),
            model_hint="trace-model",
            logical_scope_id="pal:resident",
            metadata={"preferred_endpoint_id": "trace-endpoint"},
        )
        runtime.prompt_cache_eligible_anchor_request = runtime.prompt_cache_confirmed_anchor_request = _stub_anchor_reader(
            anchor, ids["q2"])
        ex = _executor(memory, runtime)

        result = _compact(ex, memory)
        self.assertTrue(result.success, getattr(result, "failures", None))
        payload = json.dumps(dict(transport.captured[0].payload))
        self.assertNotIn("ANCHOR-BASE", payload,
                         "an anchor whose prefix crosses the cut must not "
                         "back the warm replay")


class LeftBoundaryReplayBuilderTests(unittest.TestCase):
    """Unit level: the replay builder honors the left boundary."""

    def _executor(self, memory, runtime):
        return _executor(memory, runtime)

    def test_right_side_excluded_from_suffix(self):
        memory, ids = _memory_with_left_and_right()
        transport = CapturingTransport([])
        runtime = _runtime(transport)
        anchor = _anchor_request(ids)
        runtime.prompt_cache_eligible_anchor_request = runtime.prompt_cache_confirmed_anchor_request = _stub_anchor_reader(
            anchor, ids["a1"])
        ex = self._executor(memory, runtime)

        left_ids = tuple(
            m.message_id for m in memory.history_root.left_messages()
        )
        replay, dialect, wire = ex._resident_compaction_replay_request(
            memory,
            llm_runtime=runtime,
            logical_scope_id="pal:resident",
            preferred_endpoint_id=None,
            preferred_model_id=None,
            include_active=True,
            active_turn_id="T",
            left_message_ids=left_ids,
        )
        self.assertIsNotNone(replay)
        self.assertEqual(dialect, "openrouter_openai_explicit")
        self.assertEqual(wire, "openai_completion")
        replay_ids = [m.message_id for m in replay.messages]
        self.assertNotIn(ids["q2"], replay_ids,
                         "R content must not ride the suffix")
        # Anchor prefix rides verbatim; no suffix beyond the anchor inside L.
        self.assertEqual(replay_ids, [m.message_id for m in anchor.messages])

    def test_anchor_prefix_mismatch_refuses_warm(self):
        """Anchor whose cached prefix is not exactly L-up-to-anchor refuses."""
        memory, ids = _memory_with_left_and_right()
        transport = CapturingTransport([])
        runtime = _runtime(transport)
        # Anchor prefix skips q1 (starts at a1): not the L prefix.
        anchor = LLMRequestIR(
            messages=(
                LLMMessageIR(role=MessageRole.SYSTEM,
                             parts=(TextPartIR("ANCHOR-BASE"),),
                             message_id="anchor-system"),
                LLMMessageIR(role=MessageRole.ASSISTANT,
                             parts=(TextPartIR("A1 answer"),),
                             message_id=ids["a1"]),
            ),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=1024),
            model_hint="trace-model",
            logical_scope_id="pal:resident",
            metadata={"preferred_endpoint_id": "trace-endpoint"},
        )
        runtime.prompt_cache_eligible_anchor_request = runtime.prompt_cache_confirmed_anchor_request = _stub_anchor_reader(
            anchor, ids["a1"])
        ex = self._executor(memory, runtime)

        left_ids = tuple(
            m.message_id for m in memory.history_root.left_messages()
        )
        replay, _, _ = ex._resident_compaction_replay_request(
            memory,
            llm_runtime=runtime,
            logical_scope_id="pal:resident",
            preferred_endpoint_id=None,
            preferred_model_id=None,
            include_active=False,
            active_turn_id="",
            left_message_ids=left_ids,
        )
        self.assertIsNone(replay,
                          "coverage proof (W02/W03) must refuse a prefix that "
                          "does not cover L exactly")

    def test_suffix_carries_left_content_after_anchor(self):
        from pal.memory import MemoryService

        memory = MemoryService()
        memory.begin_l1_turn("t1", user_message=user("Q1", "q1"))
        memory.upsert_l1_assistant("t1", assistant("A1 answer", "a1"))
        memory.settle_l1_turn("t1")
        memory.begin_l1_turn("t2", user_message=user("L2", "l2"))
        memory.upsert_l1_assistant("t2", assistant("L2 answer", "l2a"))
        memory.settle_l1_turn("t2")
        messages = [m for t in memory.l1_store.turns.turns for m in t.messages]
        ids = {"q1": messages[0].message_id, "a1": messages[1].message_id,
               "l2": messages[2].message_id, "l2a": messages[3].message_id}
        # L covers both settled turns; the (nonexistent) R is empty.
        left_ids = (ids["q1"], ids["a1"], ids["l2"], ids["l2a"])

        transport = CapturingTransport([])
        runtime = _runtime(transport)
        anchor = _anchor_request(ids)
        runtime.prompt_cache_eligible_anchor_request = runtime.prompt_cache_confirmed_anchor_request = _stub_anchor_reader(
            anchor, ids["a1"])
        ex = self._executor(memory, runtime)

        replay, _, _ = ex._resident_compaction_replay_request(
            memory,
            llm_runtime=runtime,
            logical_scope_id="pal:resident",
            preferred_endpoint_id=None,
            preferred_model_id=None,
            include_active=False,
            active_turn_id="",
            left_message_ids=left_ids,
        )
        self.assertIsNotNone(replay)
        replay_ids = [m.message_id for m in replay.messages]
        self.assertIn(ids["l2"], replay_ids,
                      "L content after the anchor rides the suffix")
        self.assertIn(ids["l2a"], replay_ids)
        self.assertEqual(replay_ids[-1], ids["l2a"])


if __name__ == "__main__":
    unittest.main()
