"""G2 acceptance: two-segment projection semantics (pal-two-segment-v3).

Maps TEST_MATRIX P01-P08 (codec oracle, preamble, seams, native ownership,
left rebase) and the session-level half of W01-W08 (handoff view isolation,
envelope handling, single-shot encode) against the real Completion /
Responses / Anthropic codecs.

Engine-level W facts (retry budgets, deadlines, cache hit observation) ride
the inherited v2 warm/cold suite; the session-side facts are pinned here.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    MessageState,
    TextPartIR,
    WireShape,
)
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    OwnerFence,
)
from pal.llm.projection_session import (
    EndpointProjectionSession,
    HistoryView,
    LeftReplacement,
    ProjectionSessionError,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext

SHAPES = (WireShape.OPENAI_COMPLETION, WireShape.OPENAI_RESPONSE,
          WireShape.ANTHROPIC_MESSAGES)

CONTAINERS = {
    WireShape.OPENAI_COMPLETION: "messages",
    WireShape.OPENAI_RESPONSE: "input",
    WireShape.ANTHROPIC_MESSAGES: "messages",
}


def _binding(shape: WireShape) -> EndpointBinding:
    return EndpointBinding(
        endpoint_id="endpoint-1",
        model_id="model-1",
        wire_shape=shape,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint="fp-1",
    )


def _attempt(session: EndpointProjectionSession, attempt_id: str) -> AttemptKey:
    return AttemptKey(
        identity=session.identity,  # type: ignore[arg-type]
        owner_fence=OwnerFence(0),
        attempt_id=attempt_id,
    )


def _user(text: str, message_id: str = "") -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.USER,
        parts=(TextPartIR(text),),
        **({"message_id": message_id} if message_id else {}),
    )


def _assistant(text: str, message_id: str = "") -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.ASSISTANT,
        parts=(TextPartIR(text),),
        state=MessageState.COMPLETE,
        **({"message_id": message_id} if message_id else {}),
    )


def _seed(text: str) -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.ASSISTANT,
        parts=(TextPartIR(text),),
        semantic_kind="runtime_context_summary",
        message_id="seed-msg",
    )


def _cursor(seq: int, digest: str, epoch: int = 0) -> HistoryCursor:
    return HistoryCursor(history_epoch=epoch, block_sequence=seq, prefix_digest=digest)


def _receipt(attempt: AttemptKey, before: HistoryCursor, after: HistoryCursor) -> HistoryCommitReceipt:
    return HistoryCommitReceipt(
        attempt=attempt,
        append=AppendReceipt(before=before, after=after, block_count=1),
        closed_call_ids=(),
        native_committed=False,
    )


def _items(request, shape: WireShape) -> list[dict]:
    import json

    return json.loads(request.payload_json)[CONTAINERS[shape]]


def _full_encode(shape: WireShape, messages) -> dict:
    codec = codec_for_shape(shape)
    context = ShapeContext(
        wire_shape=shape, endpoint_id="endpoint-1", model_id="model-1"
    )
    request = LLMRequestIR(
        messages=tuple(messages), tools=(),
        policy=GenerationPolicyIR(max_output_tokens=4096),
    )
    return dict(codec.encode(request, context).payload)


class _Lineage:
    """Drives a session through rounds with span-carrying commits."""

    def __init__(self, shape: WireShape) -> None:
        self.shape = shape
        self.session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        self.session.bind(_binding(shape))
        self.seq = 0
        self.epoch = 0

    def round_trip(self, messages, attempt_id: str) -> list[dict]:
        self.seq += 1
        before = self.session.frontier
        self.session.begin_round(_attempt(self.session, attempt_id), requires_native=False)
        request = self.session.prepare_normal(
            HistoryView(cursor=before, messages=tuple(messages))
        )
        after = _cursor(self.seq, f"d{self.seq}" * 32, self.epoch)
        self.session.observe_commit(
            _receipt(_attempt(self.session, attempt_id), before, after),
            span_message_ids=[m.message_id for m in messages],
        )
        return _items(request, self.shape)

    def rebase_left(self, seed_messages, kept_frozen_messages):
        self.seq += 1
        self.epoch += 1
        self.session.on_left_replaced(
            LeftReplacement(
                seed_messages=tuple(seed_messages),
                kept_frozen_messages=tuple(kept_frozen_messages),
                cursor_after=_cursor(self.seq, f"d{self.seq}" * 32, self.epoch),
                left_revision=1,
            )
        )


class TwoSegmentOracleTests(unittest.TestCase):
    def test_P01_incremental_equals_full_across_shapes_and_rebase(self):
        for shape in SHAPES:
            with self.subTest(shape=shape.value):
                lineage = _Lineage(shape)
                left_round = (_user("L question", "lu1"), _assistant("L answer", "la1"))
                right_round = (_user("R question", "ru1"), _assistant("R answer", "ra1"))
                tail_round = (_user("tail question", "tu1"), _assistant("tail answer", "ta1"))
                lineage.round_trip(left_round, "a1")
                lineage.round_trip(right_round, "a2")
                # Compact install: old L round summarized; R round survives.
                seed = (_seed("SUMMARY of L round"),)
                lineage.rebase_left(seed, right_round)
                lineage.session.begin_round(
                    _attempt(lineage.session, "a3"), requires_native=False)
                request = lineage.session.prepare_normal(
                    HistoryView(cursor=lineage.session.frontier, messages=tail_round)
                )
                incremental = _items(request, shape)
                full = _full_encode(
                    shape, seed + right_round + tail_round)[CONTAINERS[shape]]
                self.assertEqual(incremental, full)

    def test_P02_preamble_once_and_never_frozen(self):
        shell = LLMRequestIR(
            messages=(
                LLMMessageIR(
                    role=MessageRole.SYSTEM,
                    parts=(TextPartIR("You are Pal."),),
                    message_id="sys-1",
                ),
            ),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        for shape in SHAPES:
            with self.subTest(shape=shape.value):
                lineage = _Lineage(shape)
                r1 = (_user("q1", "u1"), _assistant("a1", "s1"))
                lineage.session.begin_round(
                    _attempt(lineage.session, "x1"), requires_native=False)
                first = lineage.session.prepare_normal(
                    HistoryView(cursor=lineage.session.frontier, messages=r1),
                    request_shell=shell,
                )
                items = _items(first, shape)
                blob = repr(items)
                import json

                if shape is WireShape.ANTHROPIC_MESSAGES:
                    self.assertEqual(
                        json.loads(first.payload_json)["system"],
                        [{"type": "text", "text": "You are Pal."}],
                    )
                else:
                    self.assertEqual(blob.count("You are Pal."), 1)
                after = _cursor(1, "d1" * 32, 0)
                lineage.session.observe_commit(
                    _receipt(_attempt(lineage.session, "x1"),
                             HistoryCursor.initial(), after),
                    span_message_ids=[m.message_id for m in r1],
                )
                # The frozen chunk never contains the shell system message.
                frozen_blob = repr([
                    item for chunk in lineage.session.chunks for item in chunk.items
                ])
                self.assertNotIn("You are Pal.", frozen_blob)
                # After a left rebase the shell still appears exactly once.
                lineage.rebase_left((_seed("S"),), r1)
                lineage.session.begin_round(
                    _attempt(lineage.session, "x2"), requires_native=False)
                second = lineage.session.prepare_normal(
                    HistoryView(cursor=lineage.session.frontier, messages=()),
                    request_shell=shell,
                )
                second_blob = repr(_items(second, shape))
                if shape is not WireShape.ANTHROPIC_MESSAGES:
                    self.assertEqual(second_blob.count("You are Pal."), 1)

    def test_P03_anthropic_user_seam_survives_rebase(self):
        shape = WireShape.ANTHROPIC_MESSAGES
        lineage = _Lineage(shape)
        r1 = (_user("q1", "u1"), _assistant("a1", "s1"))
        r2 = (_user("q2", "u2"), _user("q3", "u3"))
        lineage.round_trip(r1, "a1")
        lineage.round_trip(r2, "a2")
        lineage.rebase_left((_seed("S"),), r2)
        tail = (_user("q4", "u4"),)
        lineage.session.begin_round(
            _attempt(lineage.session, "a3"), requires_native=False)
        request = lineage.session.prepare_normal(
            HistoryView(cursor=lineage.session.frontier, messages=tail)
        )
        incremental = _items(request, shape)
        full = _full_encode(
            shape, (_seed("S"),) + r2 + tail)[CONTAINERS[shape]]
        self.assertEqual(incremental, full)

    def test_P05_rebase_keeps_right_wire_and_native(self):
        from pal.llm.projection_session import HistoryView  # noqa: F401

        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        r1 = (_user("L q", "lu"), _assistant("L a", "la"))
        r2 = (_user("R q", "ru"), _assistant("R a", "ra"))
        lineage.round_trip(r1, "a1")
        lineage.round_trip(r2, "a2")
        native_payload = {"messages": [{"role": "assistant", "content": "R a native"}]}
        lineage.session.native_by_attempt["a2"] = {
            "call_ids": [],
            "payload_json": __import__("json").dumps(native_payload),
        }
        right_wire_before = [
            dict(item) for chunk in lineage.session.chunks
            for item in chunk.items if "R a" in repr(item)
        ]
        lineage.rebase_left((_seed("S"),), r2)
        # bind()-style destruction must NOT happen: R wire items and native
        # survive the rebase in the same binding.
        self.assertIn("a2", lineage.session.native_by_attempt)
        right_wire_after = [
            dict(item) for chunk in lineage.session.chunks
            for item in chunk.items if "R a" in repr(item)
        ]
        self.assertTrue(right_wire_after)
        self.assertEqual(repr(right_wire_before), repr(right_wire_after))
        self.assertEqual(lineage.session.identity.projection_generation, 0)

    def test_P06_endpoint_switch_differs_from_rebase(self):
        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        r1 = (_user("q1", "u1"), _assistant("a1", "s1"))
        lineage.round_trip(r1, "a1")
        lineage.session.native_by_attempt["a1"] = {
            "call_ids": [], "payload_json": "{}"
        }
        # A real endpoint switch destroys the lineage including R material.
        lineage.session.rebind(_binding(shape))
        self.assertEqual(lineage.session.chunks, ())
        self.assertEqual(lineage.session.native_by_attempt, {})
        self.assertEqual(lineage.session.frontier, HistoryCursor.initial())

    def test_P07_empty_tail_and_seed_only_view(self):
        for shape in SHAPES:
            with self.subTest(shape=shape.value):
                lineage = _Lineage(shape)
                lineage.rebase_left((_seed("S"),), ())
                lineage.session.begin_round(
                    _attempt(lineage.session, "h1"), requires_native=False)
                request = lineage.session.prepare_normal(
                    HistoryView(cursor=lineage.session.frontier, messages=())
                )
                items = _items(request, shape)
                full = _full_encode(shape, (_seed("S"),))[CONTAINERS[shape]]
                self.assertEqual(items, full)

    def test_P08_handoff_leaves_normal_lineage_untouched(self):
        shape = WireShape.OPENAI_RESPONSE
        lineage = _Lineage(shape)
        left_round = (_user("L q", "lu"), _assistant("L a", "la"))
        lineage.round_trip(left_round, "a1")
        frontier_before = lineage.session.frontier
        chunks_before = lineage.session.chunks
        prefix_before = list(lineage.session._prefix_items)
        instruction = _user("Summarize the conversation above.", "handoff-instr")
        lineage.session.prepare_handoff(
            left_round,
            instruction=instruction,
            attempt=_attempt(lineage.session, "handoff-1"),
        )
        self.assertEqual(lineage.session.frontier, frontier_before)
        self.assertEqual(lineage.session.chunks, chunks_before)
        self.assertEqual(lineage.session._prefix_items, prefix_before)
        self.assertIsNone(lineage.session._active)

    def test_handoff_requires_closed_normal_round(self):
        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        lineage.session.begin_round(
            _attempt(lineage.session, "open"), requires_native=False)
        with self.assertRaises(ProjectionSessionError):
            lineage.session.prepare_handoff(
                (_user("L", "lu"),),
                instruction=_user("instr", "i1"),
                attempt=_attempt(lineage.session, "handoff-1"),
            )


class HandoffRequestTests(unittest.TestCase):
    def test_W_left_only_with_instruction(self):
        for shape in SHAPES:
            with self.subTest(shape=shape.value):
                lineage = _Lineage(shape)
                left = (_user("L q1", "lu1"), _assistant("L a1", "la1"),
                        _user("L q2", "lu2"), _assistant("L a2", "la2"))
                right = (_user("R private", "ru9"),)
                instruction = _user("Produce the summary now.", "instr")
                request = lineage.session.prepare_handoff(
                    left, instruction=instruction,
                    attempt=_attempt(lineage.session, "handoff-1"),
                )
                items = _items(request, shape)
                blob = repr(items)
                self.assertIn("L q1", blob)
                self.assertIn("L q2", blob)
                self.assertIn("Produce the summary now.", blob)
                self.assertNotIn("R private", blob)
                full = _full_encode(shape, left + (instruction,))[CONTAINERS[shape]]
                self.assertEqual(items, full)

    def test_W04_shell_policy_carried_verbatim(self):
        shape = WireShape.ANTHROPIC_MESSAGES
        lineage = _Lineage(shape)
        from pal.llm.ir import ThinkingLevel

        shell = LLMRequestIR(
            messages=(),
            tools=(),
            policy=GenerationPolicyIR(
                max_output_tokens=4096,
                thinking_level=ThinkingLevel.HIGH,
                thinking_budget_tokens=1024,
            ),
        )
        request = lineage.session.prepare_handoff(
            (_user("L", "lu"),),
            instruction=_user("instr", "i1"),
            attempt=_attempt(lineage.session, "handoff-1"),
            request_shell=shell,
        )
        import json

        payload = json.loads(request.payload_json)
        self.assertEqual(payload.get("max_tokens"), 4096)
        self.assertEqual(payload.get("thinking", {}).get("type"), "enabled")
        self.assertEqual(payload.get("thinking", {}).get("budget_tokens"), 1024)

    def test_W05_tools_schema_stays_in_envelope(self):
        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        from pal.shared.tool_protocol import ToolDefinitionIR

        shell = LLMRequestIR(
            messages=(),
            tools=(ToolDefinitionIR(
                name="read_file", description="Read a file",
                input_schema={"type": "object", "properties": {}}),),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        request = lineage.session.prepare_handoff(
            (_user("L", "lu"), _user("L2", "lu2")),
            instruction=_user("instr", "i1"),
            attempt=_attempt(lineage.session, "handoff-1"),
            request_shell=shell,
        )
        import json

        payload = json.loads(request.payload_json)
        self.assertEqual([t["function"]["name"] for t in payload["tools"]],
                         ["read_file"])
        blob = repr(payload["messages"])
        self.assertEqual(blob.count("Read a file"), 0)  # schema not inlined

    def test_rebase_refuses_spanless_chunks(self):
        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        r1 = (_user("q", "u1"), _assistant("a", "s1"))
        lineage.session.begin_round(
            _attempt(lineage.session, "legacy"), requires_native=False)
        lineage.session.prepare_normal(
            HistoryView(cursor=HistoryCursor.initial(), messages=r1))
        after = _cursor(1, "d1" * 32)
        lineage.session.observe_commit(  # legacy: no span ids supplied
            _receipt(_attempt(lineage.session, "legacy"),
                     HistoryCursor.initial(), after)
        )
        with self.assertRaises(ProjectionSessionError):
            lineage.rebase_left((_seed("S"),), r1)

    def test_rebase_preserves_precisely_owned_output_after_input_retires(self):
        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        r1 = (_user("q", "u1"), _assistant("a", "s1"))
        lineage.round_trip(r1, "a1")
        # Request admission may freeze the input before output is sent.
        lineage.rebase_left((_seed("S"),), (r1[1],))
        self.assertEqual(lineage.session.frozen_message_ids(), ("s1",))

    def test_rebase_refuses_ambiguous_partial_chunk(self):
        lineage = _Lineage(WireShape.OPENAI_COMPLETION)
        messages = (_user("q", "u1"), _assistant("a", "s1"))
        lineage.round_trip(messages, "a1")
        chunk = lineage.session.chunks[0]
        lineage.session.chunks = (replace(
            chunk,
            item_spans=(("u1", "s1"),) * len(chunk.items),
            item_block_spans=(),
        ),)
        with self.assertRaises(ProjectionSessionError):
            lineage.rebase_left((_seed("S"),), (messages[1],))

    def test_rebase_refuses_mismatched_kept_messages(self):
        shape = WireShape.OPENAI_COMPLETION
        lineage = _Lineage(shape)
        r1 = (_user("q", "u1"), _assistant("a", "s1"))
        lineage.round_trip(r1, "a1")
        foreign = (_user("never committed", "ghost"),)
        with self.assertRaises(ProjectionSessionError):
            lineage.rebase_left((_seed("S"),), foreign)


if __name__ == "__main__":
    unittest.main()
