"""4b14ce4 review closure (B1): bootstrap coverage of the whole L view.

B1 · `_install_left_replacement` encodes the WHOLE current L model view for a
     recovery/rebind bootstrap, so its coverage must be that view's full id
     set — not just the standalone continuity reference.  With the old
     one-id coverage every promoted ordinary message was re-appended as
     fresh tail and the wire carried it twice.

Matrix mapping (review TEST_MATRIX.md):
- B01 → the strengthened rebind node in tests/test_v3_c9cb2d2_review_fixes.py
  (test_C02...) pins per-request wire multiplicity on the existing scenario.
- B02 → this file: real app checkpoint save/restore with post-compact
  ordinary history promoted, real PromptCompiler path, two rounds.
- B03 → existing controls: C01b (seed-only app positive), C03 gen0/stale
  nodes in tests/test_v3_c9cb2d2_review_fixes.py stay green and unweakened.
- B04 → this file: seed + two promoted groups, second group in the SAME
  logical turn as the current R; every id once; the closed cut stays legal.
- B05 → this file: a complete aggregated tool call + result inside the
  promoted left, current work in R, all three supported shapes; prepare-only
  (no network at all) so nothing can re-execute a tool.
- B06 → this file: bootstrap, ordinary rounds, then a second real compact;
  the old L coverage retires with the old L, the new summary and current R
  ride once, successors keep using the projection (no prewarm).

Real components: MemoryService/HistoryRoot, the resident-checkpoint app
harness, the real PromptCompiler executor path, LLMRuntime +
EndpointProjectionSession, the real compaction engine for the second
compact.  Faked: network frames only.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core.turns import LLMRequestEffect
from pal.llm.capabilities import register_with_core as register_llm_with_core
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.shared import PromptAssemblyContext
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR, new_tool_call

from tests.test_resident_checkpoint import _build_app
from tests.test_runtime_compaction import _valid_pal_payload
from tests.test_v3_c9cb2d2_review_fixes import (
    compact,
    history,
    _model_view_request,
    valid_response,
)
from tests.test_v3_n1_root_lifecycle import assistant, user
from tests.test_v3_n3_vertical_trace import (
    CapturingTransport,
    _drive,
    _executor,
    _runtime,
)


def _ok(test: unittest.TestCase, outcome) -> None:
    test.assertEqual(
        str(getattr(outcome.status, "value", outcome.status)), "ok"
    )


def _blob(payload) -> str:
    return json.dumps(thaw_json(payload), ensure_ascii=False)


class _Settings:
    def get_active_llm_endpoint_id(self):
        return "shape-endpoint"

    def get_think_level(self, _endpoint_id):
        return "off"


def _endpoint_shape(shape: str) -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id="shape-endpoint", provider="openai", model_id=f"m-{shape}",
        display_name=shape, wire_shape=shape,
        base_url="https://example.test/v1", auth_kind="api_key_ref",
        credential_ref="key", context_window=100_000, max_output_tokens=4_096,
        thinking_levels_blob=["off"], default_thinking_level="off",
        supports_tools=True, supports_streaming=False, supports_vision=False,
        input_modalities_blob=["text"], output_modalities_blob=["text"],
        priority=0, enabled=True, capabilities_blob={},
    )


class _Continuation(SimpleNamespace):
    def __getattr__(self, name):
        return None


def _continuation() -> _Continuation:
    return _Continuation(
        budget_failure_feedback_text="", llm_round_index=0,
        preferred_llm_endpoint_id=None, preferred_llm_model_id=None,
        finalization_only=False, finalization_attempted=False,
        tool_observations=[], last_response_mode=None, turn_id="T",
        delivery_binding=None, interrupted=False, turn_settings_snapshot={},
    )


class BootstrapCoverageTests(unittest.TestCase):
    def test_B02_restore_after_post_compact_history_promoted_keeps_each_message_once(self):
        """The app's own executor + PromptCompiler after a real restore, with
        ordinary history promoted into L between compact and shutdown."""

        with tempfile.TemporaryDirectory() as directory:
            app_a = _build_app(Path(directory))
            source = app_a.handle.memory_service
            source.begin_l1_turn("closed", user_message=user("Original task", "closed-q"))
            source.upsert_l1_assistant(
                "closed", assistant("Original reply", "closed-a"))
            source.settle_l1_turn("closed")
            source.history_root.promote()

            summary = "RESTORE_LEFT_SUMMARY_4B14"
            result, _provider = compact(source, valid_response(summary))
            self.assertTrue(result.success, result.failures)

            # Real work happened AFTER compaction and BEFORE normal shutdown.
            source.begin_l1_turn("post-compact", user_message=user(
                "POST_COMPACT_USER_4B14", "post-q"))
            source.upsert_l1_assistant("post-compact", assistant(
                "POST_COMPACT_ANSWER_4B14", "post-a"))
            source.settle_l1_turn("post-compact")
            source.history_root.promote()
            self.assertEqual(source.history_root.left_generation, 1)
            self.assertTrue({"post-q", "post-a"} <= {
                m.message_id for m in source.history_root.left_messages()
            })

            asyncio.run(app_a._checkpoint_for_shutdown_async())
            self.assertEqual(app_a.last_checkpoint_status, "l1_saved",
                             app_a.last_checkpoint_error)
            app_b = _build_app(Path(directory))
            asyncio.run(app_b._restore_checkpoint_async())
            self.assertEqual(app_b.last_checkpoint_status, "restored")
            restored = app_b.handle.memory_service
            self.assertEqual(restored.history_root.left_generation, 1)

            transport = CapturingTransport(["NEW_ANSWER_1_4B14", "NEW_ANSWER_2_4B14"])
            runtime = _runtime(transport)
            register_llm_with_core(app_b.handle.core.context, runtime)
            executor = app_b.handle.core.turn_executor

            def drive():
                return asyncio.run(executor._handle_llm_request(
                    LLMRequestEffect(assembly_context=PromptAssemblyContext()),
                    _continuation(),
                ))

            try:
                restored.begin_l1_turn("T", user_message=user(
                    "AFTER_RESTART_USER_1_4B14", "new-q1"))
                _ok(self, drive())
                restored.append_l1_user("T", user(
                    "AFTER_RESTART_USER_2_4B14", "new-q2"))
                _ok(self, drive())

                self.assertEqual(len(transport.captured), 2)
                for index, request in enumerate(transport.captured):
                    text = _blob(request.payload)
                    for sentinel in (summary, "POST_COMPACT_USER_4B14",
                                     "POST_COMPACT_ANSWER_4B14",
                                     "AFTER_RESTART_USER_1_4B14"):
                        with self.subTest(request_number=index + 1, sentinel=sentinel):
                            self.assertEqual(
                                text.count(sentinel), 1,
                                "every represented message occurs exactly once")
                session = runtime.endpoint_projection_session("pal:resident")
                self.assertEqual(session.history_left_revision, 1)
                self.assertEqual(len(session.chunks), 2)
            finally:
                runtime.close()

    def test_B04_promoted_groups_and_same_turn_r_ride_each_once(self):
        """Seed + two promoted ordinary groups; the second group and the
        current R live in the SAME logical turn.  Every id rides once and
        the closed cut stays legal across the restore."""

        service = history()
        result, _ = compact(service, valid_response("B04 SEED"))
        self.assertTrue(result.success, result.failures)

        transport = CapturingTransport(["A1 answer", "A2 answer"])
        runtime = _runtime(transport)
        executor = _executor(service, runtime)
        try:
            service.begin_l1_turn("T", user_message=user("B04 Q1", "b04-q1"))
            _ok(self, _drive(executor, _model_view_request(service)))
            service.append_l1_user("T", user("B04 Q2", "b04-q2"))
            _ok(self, _drive(executor, _model_view_request(service)))
            # The next request boundary closes the second group: both groups
            # move into L while nothing new waits on the right yet.
            # Simulate admission of the next request before its output arrives.
            root = service.history_root
            submitted = root.prepare_submission(m.message_id for m in root.right_messages())
            self.assertTrue(root.commit_submission(submitted, submitted.required_ids))
            left_ids = [m.message_id for m in service.history_root.left_messages()]
            self.assertIn("b04-q1", left_ids)
            self.assertIn("b04-q2", left_ids)
            self.assertEqual(service.history_root.right_messages(), ())
        finally:
            runtime.close()

        payload = MemoryRuntimeStatePort(service).snapshot_state()
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))
        self.assertEqual(
            [m.message_id for m in restored.history_root.left_messages()],
            left_ids,
        )

        # Current R is appended in the SAME logical turn as the second group.
        restored.append_l1_user("T", user("B04 Q3", "b04-q3"))
        turn_messages = {m.message_id for m in restored.l1_store.turns.get("T").messages}
        self.assertTrue({"b04-q2", "b04-q3"} <= turn_messages,
                        "second promoted group and current R share logical turn T")

        transport2 = CapturingTransport(["A3 answer"])
        runtime2 = _runtime(transport2)
        executor2 = _executor(restored, runtime2)
        try:
            _ok(self, _drive(executor2, _model_view_request(restored)))
            sent = _blob(transport2.captured[0].payload)
            for sentinel in ("B04 SEED", "B04 Q1", "A1 answer",
                             "B04 Q2", "A2 answer", "B04 Q3"):
                with self.subTest(sentinel=sentinel):
                    self.assertEqual(
                        sent.count(sentinel), 1,
                        "promoted history and current R must each ride once")
        finally:
            runtime2.close()

    def test_B05_tool_group_in_promoted_left_rides_each_shape_once(self):
        """A complete aggregated tool call + result inside the promoted left,
        current work in R, all three supported shapes.  Prepare-only: no
        transport is ever sent, so no tool can be re-executed."""

        service = history()
        result, _ = compact(service, valid_response("B05 SEED"))
        self.assertTrue(result.success, result.failures)
        service.begin_l1_turn("T", user_message=user("Tool task start", "b05-q"))
        service.upsert_l1_assistant("T", LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(new_tool_call(
                call_id="call-b05", name="fixture_b05", arguments={"x": 1}),),
            message_id="b05-call",
        ))
        service.append_l1_tool_result("T", ToolResultIR(
            call_id="call-b05", name="fixture_b05",
            content="TOOL_RESULT_B05_SENTINEL"))
        service.settle_l1_turn("T")
        service.history_root.promote()
        service.begin_l1_turn("T2", user_message=user("New work in R", "b05-r"))

        markers = {
            "openai_completion": ('"tool_calls"', '"tool_call_id"'),
            "openai_response": ('"function_call"',),
            "anthropic_messages": ('"tool_use"', '"tool_result"'),
        }
        for shape, shape_markers in markers.items():
            with self.subTest(shape=shape):
                runtime = LLMRuntime(
                    EndpointResolver(endpoints=(_endpoint_shape(shape),)),
                    _Settings(),
                    endpoint_invoker=SimpleNamespace(),
                    config=SimpleNamespace(
                        runtime_root=tempfile.mkdtemp(),
                        llm_endpoint_retry_attempts=1,
                        llm_max_output_recovery_attempts=0,
                    ),
                )
                runtime.active_endpoint_id = "shape-endpoint"
                executor = _executor(service, runtime)
                try:
                    pack = executor._prepare_turn_projection(
                        runtime, _model_view_request(service))
                    self.assertIsNotNone(
                        pack, "the fresh bootstrap must engage for every shape")
                    blob = _blob(pack[0].payload)
                    self.assertEqual(blob.count("TOOL_RESULT_B05_SENTINEL"), 1)
                    self.assertEqual(
                        blob.count("call-b05"), 2,
                        "declaration + result linkage, never a second call")
                    self.assertLess(
                        blob.find("call-b05"),
                        blob.find("TOOL_RESULT_B05_SENTINEL"),
                        "the call must precede its result")
                    for sentinel in ("B05 SEED", "Tool task start", "New work in R"):
                        self.assertEqual(blob.count(sentinel), 1)
                    for marker in shape_markers:
                        self.assertIn(marker, blob,
                                      f"{shape} must encode through its own codec")
                    self.assertEqual(
                        tuple(pack[2]["tail_ids"]), ("b05-r",),
                        "only the current R rides the tail; the tool group is "
                        "materialized once in the rebuilt prefix")
                    kinds = [d.get("kind") for d in executor.state.diagnostics]
                    self.assertNotIn("two_segment_turn_projection_stale_left", kinds)
                finally:
                    runtime.close()

    def test_B06_bootstrap_then_second_compact_retires_old_left_coverage(self):
        """Bootstrap, ordinary rounds, then a second real compact: the old L
        coverage retires with the old L (the second compact's cut folds the
        closed rounds into the new summary), the new summary and the work
        that arrives after it ride once, and successors keep using the
        projection without prewarm."""

        service = history()
        result, _ = compact(service, valid_response("B06 SEED ONE"))
        self.assertTrue(result.success, result.failures)
        transport = CapturingTransport(["R1 answer", "R2 answer"])
        runtime = _runtime(transport)
        executor = _executor(service, runtime)
        try:
            service.begin_l1_turn("T", user_message=user("B06 Q1", "b06-q1"))
            _ok(self, _drive(executor, _model_view_request(service)))
            service.append_l1_user("T", user("B06 Q2", "b06-q2"))
            _ok(self, _drive(executor, _model_view_request(service)))
            # Simulate admission of the next request before its output arrives.
            root = service.history_root
            submitted = root.prepare_submission(m.message_id for m in root.right_messages())
            self.assertTrue(root.commit_submission(submitted, submitted.required_ids))
        finally:
            runtime.close()

        payload = MemoryRuntimeStatePort(service).snapshot_state()
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))

        transport2 = CapturingTransport([
            "RE1 answer",
            _valid_pal_payload("B06 SEED TWO"),
            "RE4 answer",
        ])
        runtime2 = _runtime(transport2)
        executor2 = _executor(restored, runtime2)
        try:
            restored.append_l1_user("T", user("B06 Q3", "b06-q3"))
            _ok(self, _drive(executor2, _model_view_request(restored)))
            bootstrap_sent = _blob(transport2.captured[0].payload)
            for sentinel in ("B06 SEED ONE", "B06 Q1", "R1 answer",
                             "B06 Q2", "R2 answer", "B06 Q3"):
                with self.subTest(bootstrap_sentinel=sentinel):
                    self.assertEqual(
                        bootstrap_sent.count(sentinel), 1,
                        "the bootstrap round must materialize the whole L "
                        "view exactly once")
            generation_before = restored.history_root.left_generation

            compacted = asyncio.run(executor2.compact_memory_async(
                restored,
                target_input_budget=100_000,
                reserved_output_tokens=1024,
                continuation=SimpleNamespace(turn_id="T"),
            ))
            self.assertTrue(compacted.success, compacted.failures)
            self.assertEqual(restored.history_root.left_generation,
                             generation_before + 1)

            restored.append_l1_user("T", user("B06 Q4", "b06-q4"))
            _ok(self, _drive(executor2, _model_view_request(restored)))

            sent = _blob(transport2.captured[-1].payload)
            for sentinel in ("B06 SEED TWO", "B06 Q4", "RE1 answer"):
                with self.subTest(sentinel=sentinel):
                    self.assertEqual(sent.count(sentinel), 1)
            # Retired L (including the rounds the second cut folded into the
            # new summary) must not replay as standalone wire content.
            for sentinel in ("B06 SEED ONE", "B06 Q1", "B06 Q2",
                             "B06 Q3"):
                with self.subTest(sentinel=sentinel):
                    self.assertEqual(sent.count(sentinel), 0)

            session = runtime2.endpoint_projection_session("pal:resident")
            covered = set(session.covered_message_ids())
            for retired in ("b06-q1", "b06-q2", "b06-q3"):
                with self.subTest(retired=retired):
                    self.assertNotIn(retired, covered,
                                     "old L coverage retires with the old L")
            self.assertEqual(session.history_left_revision,
                             restored.history_root.left_generation)
            # One send per round + one for the compact: no prewarm.
            self.assertEqual(len(transport2.captured), 3)
        finally:
            runtime2.close()


if __name__ == "__main__":
    unittest.main()
