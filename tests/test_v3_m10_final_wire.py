"""M10 (review 95373ef): L-replacement touches only the left segment.

Three real compact paths — success, engine failure, cancellation — each
materializes the next request and asserts on the FINAL WIRE: the right
segment's native tool call/result items survive verbatim, the left segment
is replaced only on success (summary exactly once, compacted-away content
never replays), in-flight right content is never folded into a summary, and
no path re-executes tools.  Failure/cancel leave the physical history
untouched (left content still present, no summary).
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from pal.core.turns import LLMRequestEffect
from pal.shared import PromptAssemblyContext
from pal.memory import MemoryService

from tests.test_v3_n1_root_lifecycle import user, assistant
from tests.test_v3_4b14ce4_review_fixes import _model_view_request
from tests.test_v3_n3_vertical_trace import (
    CapturingTransport, _runtime, _executor, _request_for,
)

MARKER = "M10_SUMMARY_MARKER_95373EF"


def _continuation(turn_id: str = "T2"):
    return SimpleNamespace(
        budget_failure_feedback_text="",
        llm_round_index=0,
        preferred_llm_endpoint_id=None,
        preferred_llm_model_id=None,
        finalization_only=False,
        tool_observations=[],
        last_response_mode=None,
        turn_id=turn_id,
    )

_SUMMARY_JSON = json.dumps({
    "schema": "pal.compaction.continuity.v1",
    "kind": "pal",
    "continuity": {
        "constraints": [],
        "state": [
            "m10 fixture",
            "exercise L replacement"
        ],
        "decisions": [],
        "references": []
    },
    "summary": {"summary": MARKER},
    "memory_candidates": [],
})

TOOL_RESULT_TEXT = "run_tests [call-r1] OK 3134 passed (session bunshin:task-42)"


def _seeded_memory() -> MemoryService:
    """L = one plain settled turn; R = one in-flight turn with native tools."""
    memory = MemoryService()
    memory.begin_l1_turn("T1", user_message=user("L1 QUESTION", "l1-q"))
    memory.upsert_l1_assistant("T1", assistant("L1 ANSWER", "l1-a"))
    memory.settle_l1_turn("T1")
    memory.history_root.promote()
    memory.begin_l1_turn(
        "T2",
        user_message=user("deploy the release, check CI first", "q2"))
    memory.upsert_l1_assistant("T2", assistant(
        "running the suite", "a1",
        new_tool_call(call_id="call-r1", name="run_tests",
                      arguments={"suite": "regression"})))
    memory.append_l1_tool_result("T2", ToolResultIR(
        call_id="call-r1", name="run_tests", content=TOOL_RESULT_TEXT))
    memory.append_l1_user("T2", user("INFLIGHT_FOLLOWUP_DRAFT", "q3"))
    return memory


def _final_wire_payload(ex, runtime, memory) -> str:
    """Materialize the next real request and return its wire payload."""
    request = _model_view_request(memory)
    pack = ex._prepare_turn_projection(runtime, request)
    if pack is None:
        # Honest cold fallback is legal; use the codec path directly.
        from pal.llm.shapes.base import ShapeContext
        from pal.llm.projection_session import codec_for_shape
        endpoint = runtime.active_endpoint()
        codec = codec_for_shape(endpoint.wire_shape)
        context = ShapeContext(wire_shape=endpoint.wire_shape,
                               endpoint_id=str(endpoint.endpoint_id),
                               model_id=str(endpoint.model_id),
                               has_conversation_prefix=False)
        return json.dumps(codec.encode(request, context).payload)
    payload = json.dumps(pack[0].payload)
    pack[2]["session"].reject_commit(
        pack[2]["attempt"].attempt_id, reason="test cleanup")
    return payload


def _assert_right_verbatim(test, payload: str) -> None:
    test.assertEqual(payload.count(TOOL_RESULT_TEXT), 1,
                     "the tool result rides the wire exactly once")
    test.assertEqual(payload.count("INFLIGHT_FOLLOWUP_DRAFT"), 1,
                     "in-flight right content is preserved verbatim")
    test.assertIn("call-r1", payload,
                  "the native tool call id survives compact")
    test.assertIn("run_tests", payload)


class M10ThreePathFinalWireTests(unittest.TestCase):
    def _armed_executor(self, memory, transport):
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)
        request = _request_for(memory, ())
        ex.build_turn_prompt = lambda *a, **k: request
        executions = []

        async def counting_tool(*_a, **_k):
            executions.append(1)
            raise AssertionError("compaction must never re-execute tools")

        ex._execute_tool_async = counting_tool
        return runtime, ex, executions

    def test_success_path_replaces_left_only(self):
        async def scenario():
            memory = _seeded_memory()
            transport = CapturingTransport([_SUMMARY_JSON])
            runtime, ex, executions = self._armed_executor(memory, transport)
            # Preflight compacts L before this still-unsent R enters a request.
            result = await ex.compact_memory_async(
                memory, target_input_budget=8192, reserved_output_tokens=1024,
                continuation=_continuation())
            self.assertTrue(result.success,
                            f"real summary must install: {result!r}")
            return memory, runtime, ex, executions
        memory, runtime, ex, executions = asyncio.run(scenario())
        payload = _final_wire_payload(ex, runtime, memory)
        self.assertEqual(payload.count(MARKER), 1,
                         "the new summary rides the next wire exactly once")
        self.assertNotIn("L1 QUESTION", payload,
                         "compacted-away left content must never replay")
        _assert_right_verbatim(self, payload)
        self.assertEqual(executions, [])
        runtime.close()

    def test_failure_path_leaves_history_untouched(self):
        async def scenario():
            memory = _seeded_memory()
            transport = CapturingTransport(["NOT_JSON"])
            runtime, ex, executions = self._armed_executor(memory, transport)
            # Preflight compacts L before this still-unsent R enters a request.
            result = await ex.compact_memory_async(
                memory, target_input_budget=8192, reserved_output_tokens=1024,
                continuation=_continuation())
            self.assertFalse(result.success)
            self.assertIsNone(memory.history_root.active_run,
                              "a failed run must not stay open")
            return memory, runtime, ex, executions
        memory, runtime, ex, executions = asyncio.run(scenario())
        payload = _final_wire_payload(ex, runtime, memory)
        self.assertNotIn(MARKER, payload,
                         "no summary may exist after engine failure")
        self.assertIn("L1 QUESTION", payload,
                      "failure must leave the physical left intact")
        _assert_right_verbatim(self, payload)
        self.assertEqual(executions, [])
        runtime.close()

    def test_cancel_path_leaves_history_untouched(self):
        async def scenario():
            memory = _seeded_memory()
            transport = CapturingTransport(["unused"])
            runtime, ex, executions = self._armed_executor(memory, transport)
            # Preflight compacts L before this still-unsent R enters a request.
            entered = asyncio.Event()
            real_agenerate = runtime.agenerate

            async def hanging(*a, **k):
                entered.set()
                await asyncio.Event().wait()

            runtime.agenerate = hanging
            task = asyncio.create_task(ex.compact_memory_async(
                memory, target_input_budget=8192, reserved_output_tokens=1024,
                continuation=_continuation()))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                runtime.agenerate = real_agenerate
            self.assertIsNone(memory.history_root.active_run,
                              "a cancelled run must not stay open")
            return memory, runtime, ex, executions
        memory, runtime, ex, executions = asyncio.run(scenario())
        payload = _final_wire_payload(ex, runtime, memory)
        self.assertNotIn(MARKER, payload,
                         "no summary may exist after cancellation")
        self.assertIn("L1 QUESTION", payload,
                      "cancellation must leave the physical left intact")
        _assert_right_verbatim(self, payload)
        self.assertEqual(executions, [])
        runtime.close()


if __name__ == "__main__":
    unittest.main()
