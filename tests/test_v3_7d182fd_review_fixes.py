"""7d182fd review closure (S1): cross-cut Anthropic user-seam retirement.

S1/P1 · A replacement seed materializes L wire with EMPTY item spans; the
     Anthropic user-seam merge folds that seed's trailing user blocks into
     the next right chunk's user item; a later left replacement keeps that
     chunk (its per-item span names only right ids), so the retired left's
     bytes replay — and the tool-result variant leaves an orphan result
     that the tool protocol refuses on every later prepare.

Matrix mapping (review TEST_MATRIX.md):
- S01 → reviewer T01 (component, text tail) + T04 (the interrupted
  user-only turn legally promotes into L, so the cut is not hand-made).
- S02 → THIS FILE: B05's LEFT complete tool group extended past prepare —
  accept R's answer, compact ONLY the left (R is never promoted before the
  cut, so it stays right with its chunks), then keep using the SAME
  projection lineage.  The retired tool group must vanish while R's Q/A
  and the next question each ride exactly once, with NO cold fallback.
- S03 → reviewer T03 (three-shape non-merge control) + THIS FILE's
  openai_completion vertical: the same retirement without any user-seam
  merge (tool results are their own role items there).
- S04 → reviewer T04.
- S05 → existing B02/B04/B05/B06 and the native/projection/Continuity
  suites stay green; no assertion is weakened.

Real components: MemoryService/HistoryRoot, the real compaction engine for
BOTH compacts, LLMRuntime + EndpointProjectionSession through the real
TurnExecutor drive path, per-shape codecs, and the tool protocol
validator.  Faked: network frames only.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import _JSONFrame
from pal.shared.tool_protocol import ToolResultIR, new_tool_call

from tests.test_runtime_compaction import _valid_pal_payload
from tests.test_v3_4b14ce4_review_fixes import (
    _Settings,
    _blob,
    _endpoint_shape,
    _ok,
)
from tests.test_v3_c9cb2d2_review_fixes import (
    compact,
    history,
    _model_view_request,
    valid_response,
)
from tests.test_v3_n1_root_lifecycle import user
from tests.test_v3_n3_vertical_trace import (
    CapturingTransport,
    _drive,
    _executor,
)

_FAILURE_KINDS = (
    "two_segment_turn_projection_stale_left",
    "two_segment_turn_projection_prepare_failed",
    "two_segment_projection_rebase_failed",
    "two_segment_projection_bootstrap_failed",
)


class _AnthropicTransport:
    """Capturing transport speaking anthropic_messages response frames."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.captured = []

    def frames(self, _endpoint, transport_request):
        self.captured.append(transport_request)
        payload = {
            "content": [{
                "type": "text",
                "text": self.replies[len(self.captured) - 1],
            }],
            "stop_reason": "end_turn",
        }
        yield _JSONFrame(0, payload)

    def activate_endpoint(self, _endpoint_id):
        pass

    def close(self):
        pass


def _shape_runtime(shape: str, transport) -> LLMRuntime:
    return LLMRuntime(
        EndpointResolver(endpoints=(_endpoint_shape(shape),)),
        _Settings(),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport),
        config=SimpleNamespace(
            runtime_root=tempfile.mkdtemp(),
            llm_endpoint_retry_attempts=1,
            llm_max_output_recovery_attempts=0,
        ),
    )


def _seed_tool_group_in_left(service) -> None:
    # Turn id "tools": _drive's continuation carries turn_id="T", and the
    # executor writes R's answers there, so the right-side turn owns "T".
    service.begin_l1_turn("tools", user_message=user("Tool task start", "b05-q"))
    service.upsert_l1_assistant("tools", LLMMessageIR(
        role=MessageRole.ASSISTANT,
        parts=(new_tool_call(
            call_id="call-b05", name="fixture_b05", arguments={"x": 1}),),
        message_id="b05-call",
    ))
    service.append_l1_tool_result("tools", ToolResultIR(
        call_id="call-b05", name="fixture_b05",
        content="TOOL_RESULT_B05_SENTINEL"))
    # NO settle: settling bridges the trailing tool result with an assistant
    # message, which ends the L view on an assistant and never forms the
    # trailing user item the seam needs.  An interrupted turn keeps the tool
    # result as the LAST message (review S02's tool variant), exactly like
    # T04's interrupted user-only turn keeps its user message (S01's text
    # variant, folded into the same trailing user item by the codec's
    # adjacent-user merge).
    service.interrupt_l1_turn("tools", reason="review fixture")
    service.history_root.promote()
    service.begin_l1_turn("U", user_message=user("Old interrupted ask", "u-old"))
    service.interrupt_l1_turn("U", reason="review fixture")
    service.history_root.promote()


class CrossCutSeamRetirementTests(unittest.TestCase):
    def _run_vertical(self, *, shape: str, transport):
        """Accept R's answer, compact only the left, keep the right chunks.

        Returns the captured request payloads (round 2 and 3) plus the
        executor for post-drive diagnostics inspection.
        """

        service = history()
        result, _ = compact(service, valid_response("B05 SEED ONE"))
        self.assertTrue(result.success, result.failures)
        _seed_tool_group_in_left(service)
        # Admission freezes the input into L; the new assistant output
        # remains R. Compact must split that projection chunk precisely.
        service.begin_l1_turn("T", user_message=user("New work in R", "b05-r"))

        runtime = _shape_runtime(shape, transport)
        executor = _executor(service, runtime)
        try:
            _ok(self, _drive(executor, _model_view_request(service)))
            first = _blob(transport.captured[0].payload)
            self.assertEqual(first.count("TOOL_RESULT_B05_SENTINEL"), 1)
            self.assertEqual(
                first.count("call-b05"), 2,
                "declaration + result linkage, never a second call")

            self.assertIn("b05-r", [m.message_id for m in service.history_root.left_messages()])
            self.assertIn("R ANSWER B05", repr(service.history_root.right_messages()))
            result2, _ = compact(service, valid_response("B05 SEED TWO"),
                                 op="review-operation-2")
            self.assertTrue(result2.success, result2.failures)
            self.assertEqual(service.history_root.left_generation, 2)
            self.assertNotIn(
                "b05-r",
                [m.message_id for m in service.history_root.left_messages()],
                "the admitted input is replaced by the compact summary")
            # The L1-level compact helper bypasses the executor, so drive the
            # post-install rebase exactly like the post-commit path does
            # (same owner interface; c9cb2d2 review node pins this call).
            executor._rebase_projection_after_left_install(
                runtime, "pal:resident", service.history_root,
                memory_service=service)

            service.append_l1_user("T", user("Next question B05", "b05-r2"))
            _ok(self, _drive(executor, _model_view_request(service)))
            second = _blob(transport.captured[1].payload)
            for kind in _FAILURE_KINDS:
                self.assertNotIn(
                    kind, [d.get("kind") for d in executor.state.diagnostics],
                    f"the kept-right lineage must serve the next round warm; "
                    f"{kind} would mean an orphan refusal or a cold fallback")

            service.append_l1_user("T", user("Follow-up B05", "b05-r3"))
            _ok(self, _drive(executor, _model_view_request(service)))
            third = _blob(transport.captured[2].payload)
            session = runtime.endpoint_projection_session("pal:resident")
            self.assertEqual(session.history_left_revision, 2)
            self.assertGreater(len(session.chunks), 0)
        finally:
            runtime.close()
        return second, third

    def test_S02_left_tool_group_retires_when_second_compact_keeps_right(self):
        transport = _AnthropicTransport([
            "R ANSWER B05", "T3 ANSWER B05", "T3B ANSWER B05"])
        second, third = self._run_vertical(
            shape="anthropic_messages", transport=transport)

        for retired in ("call-b05", "TOOL_RESULT_B05_SENTINEL",
                        "Tool task start", "B05 SEED ONE",
                        "Old interrupted ask", "New work in R"):
            with self.subTest(round=2, retired=retired):
                self.assertEqual(second.count(retired), 0)
        for sentinel in ("B05 SEED TWO",
                         "R ANSWER B05", "Next question B05"):
            with self.subTest(round=2, sentinel=sentinel):
                self.assertEqual(second.count(sentinel), 1)
        for sentinel in ("B05 SEED TWO", "R ANSWER B05",
                         "Next question B05", "T3 ANSWER B05", "Follow-up B05"):
            with self.subTest(round=3, sentinel=sentinel):
                self.assertEqual(third.count(sentinel), 1)
        self.assertEqual(third.count("call-b05"), 0)

    def test_S03_openai_control_same_retirement_without_user_seam(self):
        transport = CapturingTransport([
            "R ANSWER B05", "T3 ANSWER B05", "T3B ANSWER B05"])
        second, third = self._run_vertical(
            shape="openai_completion", transport=transport)

        for retired in ("call-b05", "TOOL_RESULT_B05_SENTINEL",
                        "Tool task start", "B05 SEED ONE",
                        "Old interrupted ask", "New work in R"):
            with self.subTest(round=2, retired=retired):
                self.assertEqual(second.count(retired), 0)
        for sentinel in ("B05 SEED TWO",
                         "R ANSWER B05", "Next question B05"):
            with self.subTest(round=2, sentinel=sentinel):
                self.assertEqual(second.count(sentinel), 1)


if __name__ == "__main__":
    unittest.main()
