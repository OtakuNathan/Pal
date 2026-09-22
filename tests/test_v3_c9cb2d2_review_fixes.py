"""C9cb2d2 review closure: cold-source, handoff-acceptance, projection reentry.

C1/A01-A03 · the cold source carries every legal message whole; fit is decided
             by the real preflight send-budget check (an oversized source keeps
             the old L with the existing source_too_large verdict instead of
             silently trimming legal content).
C2/B01-B03 · the single generation-result acceptance point is TEXT ONLY: a
             TOOL_CALLS finish or tool-call parts riding next to valid JSON
             never complete a handoff; the retry spends the ordinary attempt
             budget and no unfinished call enters repair history.
C3/C01-C03 · a fresh or rebound projection cold-builds ONCE from the current
             canonical L/R after restore/rebind; a materialized stale prefix
             keeps the strict refusal (no counter-only permission).

Real components: MemoryService/HistoryRoot, the compaction engine + policy +
schema validator + install, the resident-checkpoint app harness, LLMRuntime +
EndpointProjectionSession, and the TurnExecutor path with the real
PromptCompiler (the app-level node drives the app's own executor).  Faked:
network frames only.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
)
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.turns import LLMRequestEffect
from pal.llm import generation_result_from_values
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.shared import LLMFinishReason, LLMPreflightStatus, PromptAssemblyContext
from pal.shared.tool_protocol import new_tool_call

from tests.test_runtime_compaction import _valid_pal_payload
from tests.test_v3_n1_root_lifecycle import assistant, user

SUMMARY = "ACCEPTED_REVIEW_SUMMARY"


def history(text: str = "A closed task to summarize") -> MemoryService:
    service = MemoryService()
    service.begin_l1_turn("closed", user_message=user(text, "closed-q"))
    service.upsert_l1_assistant("closed", assistant("Finished that task.", "closed-a"))
    service.settle_l1_turn("closed")
    service.history_root.promote()
    return service


class ReplyFixture:
    """Controlled provider answers; preflight answers READY (fits)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.preflights = []

    async def apreflight(self, request):
        self.preflights.append(request)
        return SimpleNamespace(
            status=LLMPreflightStatus.READY,
            target_input_budget=100_000,
            reserved_output_tokens=4096,
        )

    async def agenerate(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def compact(service, response, *, op="review-operation", max_attempts=1):
    root = service.history_root
    left = service.begin_left_compaction(op, reason="review")
    snapshot = CompactionSnapshot.capture_left(
        service, left, target_input_budget=100_000,
        reserved_output_tokens=4096,
        clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
        metadata={"compaction_op_id": op},
    )
    provider = ReplyFixture([response])
    engine = CompactionEngine(PalCompactionPolicy(), max_attempts=max_attempts,
                              timeout_seconds=5)
    result = asyncio.run(engine.run(snapshot, llm_runtime=provider,
                                    memory_service=service))
    return result, provider


def valid_response(summary: str = SUMMARY):
    return generation_result_from_values(text=_valid_pal_payload(summary))


def mixed_tool_response(*, finish=LLMFinishReason.TOOL_CALLS):
    """Valid checkpoint JSON text PLUS an independent tool-call part."""

    answer = valid_response()
    message = replace(answer.response.message, parts=(
        *answer.response.message.parts,
        new_tool_call(call_id="handoff-unexpected-call", name="fixture_noop",
                      arguments={}),
    ))
    return replace(answer, response=replace(
        answer.response, message=message, finish_reason=finish))


def sent_text(provider):
    return "\n".join(message.text for request in provider.requests
                     for message in request.messages)


def _endpoint_variant(endpoint_id: str):
    """One fake network endpoint per identity (no dataclass replace on a
    pydantic model)."""

    from pal.llm.models import LLMEndpointModel

    return LLMEndpointModel(
        endpoint_id=endpoint_id, provider="openai",
        model_id=f"trace-model-{endpoint_id}", display_name=endpoint_id,
        wire_shape="openai_completion", base_url="https://example.test/v1",
        auth_kind="api_key_ref", credential_ref="key",
        context_window=100_000, max_output_tokens=4_096,
        thinking_levels_blob=["off"], default_thinking_level="off",
        supports_tools=True, supports_streaming=False, supports_vision=False,
        input_modalities_blob=["text"], output_modalities_blob=["text"],
        priority=0, enabled=True, capabilities_blob={},
    )


class C1SourceBoundaryTests(unittest.TestCase):
    def test_A01_cold_source_carries_middle_of_a_message_that_fits(self):
        """A01: 10k head + unique middle constraint + 10k tail; the window is
        plenty.  The real engine must send the WHOLE message: installing a
        summary cannot erase L that the summarizer never received."""

        marker = "MIDDLE_ONLY_DO_NOT_DEPLOY_UNTIL_REVIEWED"
        service = history("A" * 10_000 + marker + "B" * 10_000)
        result, provider = compact(service, valid_response())
        self.assertTrue(result.success, result.failures)
        wire = sent_text(provider)
        self.assertIn(marker, wire,
                      "the summarizer must see the middle of a message that fits")
        self.assertNotIn("head/tail projection only", wire)
        # K1: the install replaced L only; R stays empty here and the new
        # left carries the accepted summary.
        root = service.history_root
        self.assertEqual(root.left_generation, 1)
        self.assertEqual(root.right_turns(), ())

    def test_A02_short_source_positive_control(self):
        """A02: short complete messages summarize normally; the sentinel is
        in the request and the left generation advances exactly once."""

        service = history("SHORT_SOURCE_SENTINEL")
        result, provider = compact(service, valid_response())
        self.assertTrue(result.success, result.failures)
        self.assertIn("SHORT_SOURCE_SENTINEL", sent_text(provider))
        self.assertEqual(service.history_root.left_generation, 1)

    def test_A03_full_source_over_window_keeps_old_l_and_refuses(self):
        """A03: when the complete source truly exceeds the window, the run
        keeps the complete old L/R and refuses with the existing verdict —
        never a source trimmed small enough to sneak through a READY window.
        """

        class WindowFixture:
            """READY while the request text fits ``window_chars``; else the
            endpoint reports COMPACT_REQUIRED — a real window simulated by
            measured request size, not by naming a code path."""

            def __init__(self, window_chars: int):
                self.window_chars = window_chars
                self.requests = []
                self.preflights = []

            def _size(self, preflight_request):
                request = getattr(preflight_request, "request", preflight_request)
                return sum(len(message.text) for message in request.messages)

            async def apreflight(self, preflight_request):
                self.preflights.append(preflight_request)
                size = self._size(preflight_request)
                status = (LLMPreflightStatus.READY
                          if size <= self.window_chars
                          else LLMPreflightStatus.COMPACT_REQUIRED)
                return SimpleNamespace(status=status,
                                       target_input_budget=100_000,
                                       reserved_output_tokens=4096)

            async def agenerate(self, request):
                self.requests.append(request)
                return valid_response()

        service = history("C" * 120_000)
        root = service.history_root
        before_turns = root.all_turns()
        before_left = tuple(m.message_id for m in root.left_messages())
        left = service.begin_left_compaction("op-a03", reason="review")
        snapshot = CompactionSnapshot.capture_left(
            service, left, target_input_budget=100_000,
            reserved_output_tokens=4096,
            clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
            metadata={"compaction_op_id": "op-a03"},
        )
        provider = WindowFixture(window_chars=50_000)
        engine = CompactionEngine(PalCompactionPolicy(), max_attempts=1,
                                  timeout_seconds=5)
        result = asyncio.run(engine.run(snapshot, llm_runtime=provider,
                                        memory_service=service))
        self.assertEqual(result.status, "source_too_large", result.failures)
        # No generation was ever attempted on the oversized source.
        self.assertEqual(provider.requests, [])
        # The complete old L/R survived untouched.
        self.assertEqual(root.left_generation, 0)
        self.assertEqual(root.all_turns(), before_turns)
        self.assertEqual(tuple(m.message_id for m in root.left_messages()),
                         before_left)


class C2HandoffAcceptanceTests(unittest.TestCase):
    def test_B01_valid_json_plus_tool_call_is_not_a_completed_handoff(self):
        """B01: TOOL_CALLS finish with a valid JSON text component is
        rejected before validation; nothing installs, no tool is dispatched."""

        service = history()
        root = service.history_root
        before = root.all_turns()
        result, provider = compact(service, mixed_tool_response())
        self.assertFalse(result.success,
                         "a valid JSON text component does not complete an "
                         "outstanding tool request")
        self.assertIn("output:tool_call_handoff", result.failures)
        self.assertEqual(root.left_generation, 0)
        self.assertEqual(root.all_turns(), before)
        # No local dispatch surface was involved: the provider was only
        # called for generation, exactly once (max_attempts=1).
        self.assertEqual(len(provider.requests), 1)

    def test_B02_tool_part_with_normal_finish_is_rejected_and_text_only_installs(self):
        """B02: the judgement does not depend on the finish name — a tool part
        under a normal STOP finish is refused; a text-only response passes."""

        service = history()
        root = service.history_root
        result, _ = compact(
            service, mixed_tool_response(finish=LLMFinishReason.STOP))
        self.assertFalse(result.success)
        self.assertIn("output:tool_call_handoff", result.failures)
        self.assertEqual(root.left_generation, 0)

        service = history()
        result, _ = compact(service, valid_response())
        self.assertTrue(result.success, result.failures)
        self.assertIn(SUMMARY, "\n".join(
            m.text for m in service.history_root.left_messages()))

    def test_B03_first_tool_handoff_rejected_second_valid_installs_once(self):
        """B03: with an ordinary attempt budget, the first illegal tool
        handoff installs nothing, the second valid answer installs once, and
        the unfinished call never enters repair history."""

        service = history()
        root = service.history_root
        left = service.begin_left_compaction("op-b03", reason="review")
        snapshot = CompactionSnapshot.capture_left(
            service, left, target_input_budget=100_000,
            reserved_output_tokens=4096,
            clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
            metadata={"compaction_op_id": "op-b03"},
        )
        provider = ReplyFixture([mixed_tool_response(), valid_response()])
        engine = CompactionEngine(PalCompactionPolicy(), max_attempts=2,
                                  timeout_seconds=5)
        result = asyncio.run(engine.run(snapshot, llm_runtime=provider,
                                        memory_service=service))
        self.assertTrue(result.success, result.failures)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(root.left_generation, 1)
        # The retry request carries no repair material derived from the
        # rejected tool-call answer.
        retry = provider.requests[1]
        semantic_kinds = {str(m.semantic_kind or "") for m in retry.messages}
        self.assertNotIn("compaction_failed_output", semantic_kinds)
        self.assertNotIn("compaction_repair_request", semantic_kinds)


def _model_view_request(memory: MemoryService, extra_tail=()):
    """Compile-equivalent request: the L1 continuity seed rides as the
    repository's actual standalone representation, not the raw assistant
    summary (the focused tests exercise reentry, not the compiler)."""

    from tests.test_v3_n3_vertical_trace import _request_for

    raw = _request_for(memory, extra_tail)
    continuity = memory.l1_store.turns.continuity
    messages = tuple(
        continuity.standalone_message()
        if continuity is not None and m.message_id == continuity.source_id else m
        for m in raw.messages
    )
    return replace(raw, messages=messages)


class C3RecoveryReentryTests(unittest.TestCase):
    def test_C01a_restored_compacted_history_reenters_projection_without_another_compact(self):
        """C01 focused: save/restore a compacted root, then two ordinary
        rounds enter AND stay in the projection without further compaction."""

        from tests.test_v3_n3_vertical_trace import (
            CapturingTransport, _runtime, _executor, _drive,
        )

        source = history()
        result, _ = compact(source, valid_response())
        self.assertTrue(result.success, result.failures)
        payload = MemoryRuntimeStatePort(source).snapshot_state()
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))
        self.assertEqual(restored.history_root.left_generation, 1)

        runtime = _runtime(CapturingTransport(["AFTER_RESTORE_1", "AFTER_RESTORE_2"]))
        executor = _executor(restored, runtime)

        def request():
            return _model_view_request(restored)

        try:
            restored.begin_l1_turn("T", user_message=user("Continue after restart.", "after-q1"))
            first = _drive(executor, request())
            self.assertEqual(str(getattr(first.status, "value", first.status)), "ok")
            restored.append_l1_user("T", user("A second normal round.", "after-q2"))
            second = _drive(executor, request())
            self.assertEqual(str(getattr(second.status, "value", second.status)), "ok")
            session = runtime.endpoint_projection_session("pal:resident")
            self.assertGreater(len(session.chunks), 0,
                               "a fresh session may cold-build once, but cannot "
                               "remain behind the restored root forever")
            self.assertEqual(session.history_left_revision,
                             restored.history_root.left_generation)
            kinds = [d.get("kind") for d in executor.state.diagnostics]
            self.assertNotIn("two_segment_turn_projection_stale_left", kinds)
        finally:
            runtime.close()

    def test_C01b_app_level_restored_compacted_root_reenters_projection(self):
        """C01 app-level: the real host saves/restores the compacted root and
        the APP'S OWN executor (real PromptCompiler path) answers two rounds;
        the summary and each user message ride the wire exactly once."""

        from pal.llm.capabilities import register_with_core as register_llm_with_core
        from tests.test_resident_checkpoint import _build_app
        from tests.test_v3_n3_vertical_trace import CapturingTransport, _runtime

        class _Continuation(SimpleNamespace):
            def __getattr__(self, name):
                return None

        def continuation(turn_id: str):
            return _Continuation(
                budget_failure_feedback_text="",
                llm_round_index=0,
                preferred_llm_endpoint_id=None,
                preferred_llm_model_id=None,
                finalization_only=False,
                finalization_attempted=False,
                tool_observations=[],
                last_response_mode=None,
                turn_id=turn_id,
                delivery_binding=None,
                interrupted=False,
                turn_settings_snapshot={},
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root_dir = Path(tmpdir)
            app_a = _build_app(root_dir)
            service = app_a.handle.memory_service
            service.begin_l1_turn("closed", user_message=user("Closed task A", "closed-q"))
            service.upsert_l1_assistant("closed", assistant("Finished A.", "closed-a"))
            service.settle_l1_turn("closed")
            service.history_root.promote()

            class _SeedNetwork:
                async def agenerate(self, request, *a, **kw):
                    return generation_result_from_values(
                        text=_valid_pal_payload("RESTORED SEED SUMMARY"))

            async def install_seed():
                left = service.begin_left_compaction("run-app", reason="review")
                snapshot = CompactionSnapshot.capture_left(
                    service, left, target_input_budget=100_000,
                    reserved_output_tokens=1024,
                    clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
                    metadata={"compaction_op_id": "run-app"},
                )
                engine = CompactionEngine(PalCompactionPolicy(),
                                          max_attempts=1, timeout_seconds=10.0)
                return await engine.run(snapshot, llm_runtime=_SeedNetwork(),
                                        memory_service=service)

            install = asyncio.run(install_seed())
            self.assertTrue(install.success, install.failures)
            self.assertEqual(service.history_root.left_generation, 1)
            asyncio.run(app_a._publish_checkpoint_async())

            app_b = _build_app(root_dir)
            asyncio.run(app_b._restore_checkpoint_async())
            self.assertEqual(app_b.last_checkpoint_status, "restored")
            restored_service = app_b.handle.memory_service
            self.assertEqual(restored_service.history_root.left_generation, 1)

            transport = CapturingTransport(["AFTER_RESTORE_1", "AFTER_RESTORE_2"])
            runtime = _runtime(transport)
            register_llm_with_core(app_b.handle.core.context, runtime)
            executor = app_b.handle.core.turn_executor

            def drive(turn_id):
                async def run():
                    return await executor._handle_llm_request(
                        LLMRequestEffect(assembly_context=PromptAssemblyContext()),
                        continuation(turn_id),
                    )
                return asyncio.run(run())

            try:
                restored_service.begin_l1_turn(
                    "T", user_message=user("Continue after restart.", "after-q1"))
                first = drive("T")
                self.assertEqual(str(getattr(first.status, "value", first.status)), "ok")
                session = runtime.endpoint_projection_session("pal:resident")
                self.assertEqual(len(session.chunks), 1,
                                 "the first ordinary round must already freeze "
                                 "through the rebuilt projection")
                self.assertEqual(session.history_left_revision, 1)

                restored_service.append_l1_user(
                    "T", user("A second normal round.", "after-q2"))
                second = drive("T")
                self.assertEqual(str(getattr(second.status, "value", second.status)), "ok")
                self.assertEqual(len(session.chunks), 2)
                self.assertEqual(session.history_left_revision, 1)

                payload1 = json.dumps(dict(transport.captured[0].payload))
                self.assertEqual(payload1.count("RESTORED SEED SUMMARY"), 1,
                                 "the restored summary rides the wire exactly once")
                self.assertEqual(payload1.count("Continue after restart."), 1)
                self.assertNotIn("Closed task A", payload1)
                payload2 = json.dumps(dict(transport.captured[1].payload))
                self.assertEqual(payload2.count("RESTORED SEED SUMMARY"), 1,
                                 "the frozen prefix replays once, never twice")
                self.assertEqual(payload2.count("Continue after restart."), 1)
                self.assertEqual(payload2.count("A second normal round."), 1)

                kinds = [d.get("kind") for d in
                         (getattr(executor.state, "diagnostics", None) or [])]
                self.assertNotIn("two_segment_turn_projection_stale_left", kinds)
                self.assertNotIn("two_segment_projection_bootstrap_failed", kinds)
            finally:
                runtime.close()

    def test_C02_endpoint_rebind_after_compact_bootstraps_once_and_reuses(self):
        """C02: after a legal endpoint switch, the rebound lineage drops the
        old native store and rebuilds a projectable view from the current
        L/R; the next round reuses it with no provider pre-warm."""

        from tests.test_v3_n3_vertical_trace import (
            CapturingTransport, _executor, _drive,
        )
        from pal.llm.endpoint import ShapeEndpointInvoker
        from pal.llm.runtime import EndpointResolver, LLMRuntime

        service = history()
        result, _ = compact(service, valid_response("REBIND SEED SUMMARY"))
        self.assertTrue(result.success, result.failures)
        root = service.history_root
        self.assertEqual(root.left_generation, 1)

        transport = CapturingTransport(["REBOUND_1", "REBOUND_2", "REBOUND_3"])
        endpoints = (_endpoint_variant("e1"), _endpoint_variant("e2"))

        class _Settings:
            def get_active_llm_endpoint_id(self):
                return "e1"

            def get_think_level(self, _endpoint_id):
                return "off"

        runtime = LLMRuntime(
            EndpointResolver(endpoints=endpoints),
            _Settings(),
            endpoint_invoker=ShapeEndpointInvoker(transport=transport),
            config=SimpleNamespace(
                runtime_root=tempfile.mkdtemp(),
                llm_endpoint_retry_attempts=1,
                llm_max_output_recovery_attempts=0,
            ),
        )
        runtime.active_endpoint_id = "e1"
        executor = _executor(service, runtime)

        try:
            service.begin_l1_turn("T", user_message=user("First round.", "rb-q1"))
            first = _drive(executor, _model_view_request(service))
            self.assertEqual(str(getattr(first.status, "value", first.status)), "ok")
            session = runtime.endpoint_projection_session("pal:resident")
            self.assertEqual(session.binding.endpoint_id, "e1")
            self.assertEqual(len(session.chunks), 1)
            self.assertEqual(session.history_left_revision, 1)
            e1_native_attempts = set(session.native_by_attempt)
            self.assertTrue(
                e1_native_attempts,
                "round 1 must capture native material for the leak check")
            self.assertTrue(
                all(record["endpoint_id"] == "e1"
                    for record in session.native_by_attempt.values()),
                "round 1 native, when captured, belongs to e1")

            # Legal endpoint switch: the next prepare rebinds the lineage.
            runtime.active_endpoint_id = "e2"
            service.append_l1_user("T", user("Second round after rebind.", "rb-q2"))
            second = _drive(executor, _model_view_request(service))
            self.assertEqual(str(getattr(second.status, "value", second.status)), "ok")
            self.assertEqual(session.binding.endpoint_id, "e2",
                             "the session must bind to the switched endpoint")
            self.assertGreaterEqual(session.identity.projection_generation, 1)
            self.assertFalse(
                e1_native_attempts & set(session.native_by_attempt),
                "old native material must not leak across providers")
            self.assertTrue(
                all(record["endpoint_id"] == "e2"
                    for record in session.native_by_attempt.values()),
                "every surviving native record belongs to the new provider")
            self.assertEqual(session.history_left_revision, 1,
                             "the rebound lineage rebuilt from the current L/R")
            self.assertEqual(len(session.chunks), 1,
                             "the post-rebind round froze through the rebuilt view")

            service.append_l1_user("T", user("Third round reuses.", "rb-q3"))
            third = _drive(executor, _model_view_request(service))
            self.assertEqual(str(getattr(third.status, "value", third.status)), "ok")
            self.assertEqual(len(session.chunks), 2,
                             "the successor round reuses the projection")
            self.assertEqual(session.history_left_revision, 1)
            # No pre-warm or extra provider traffic: exactly one send per round.
            self.assertEqual(len(transport.captured), 3)
        finally:
            runtime.close()

    def test_C03_gen0_positive_and_bootstrap_once_controls(self):
        """C03 positive controls: root gen 0 engages immediately (no cold
        build side effects), and a restored gen-1 lineage cold-builds exactly
        once — the second round must not re-enter the bootstrap."""

        from tests.test_v3_n3_vertical_trace import (
            CapturingTransport, _runtime, _executor, _drive, _request_for,
        )

        # (1) gen 0 old positive control must not regress.
        service = MemoryService()
        service.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        executor = _executor(service, runtime)
        try:
            outcome = _drive(executor, _request_for(service, ()))
            self.assertEqual(str(getattr(outcome.status, "value", outcome.status)), "ok")
            session = runtime.endpoint_projection_session("pal:resident")
            self.assertEqual(session.history_left_revision, 0)
            kinds = [d.get("kind") for d in executor.state.diagnostics]
            self.assertNotIn("two_segment_projection_bootstrap_failed", kinds)
        finally:
            runtime.close()

        # (2) restored gen 1: bootstrap exactly once across two rounds.
        source = history()
        result, _ = compact(source, valid_response())
        self.assertTrue(result.success, result.failures)
        payload = MemoryRuntimeStatePort(source).snapshot_state()
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))

        transport = CapturingTransport(["B1", "B2"])
        runtime = _runtime(transport)
        executor = _executor(restored, runtime)
        bootstrap_calls = {"count": 0}
        original = executor._bootstrap_projection_from_current_left

        def counted(*args, **kwargs):
            bootstrap_calls["count"] += 1
            return original(*args, **kwargs)

        executor._bootstrap_projection_from_current_left = counted
        try:
            restored.begin_l1_turn("T", user_message=user("After restore.", "c3-q1"))
            first = _drive(executor, _model_view_request(restored))
            self.assertEqual(str(getattr(first.status, "value", first.status)), "ok")
            self.assertEqual(bootstrap_calls["count"], 1,
                             "a fresh gen-1 lineage cold-builds exactly once")
            restored.append_l1_user("T", user("Next round.", "c3-q2"))
            second = _drive(executor, _model_view_request(restored))
            self.assertEqual(str(getattr(second.status, "value", second.status)), "ok")
            self.assertEqual(bootstrap_calls["count"], 1,
                             "the successor round must reuse the projection, "
                             "not re-bootstrap")
            session = runtime.endpoint_projection_session("pal:resident")
            self.assertEqual(len(session.chunks), 2)
            self.assertEqual(session.history_left_revision, 1)
        finally:
            runtime.close()

    def test_C03_materialized_stale_prefix_stays_refused_until_explicit_rebase(self):
        """C03 negative control: a lineage that already holds old wire content
        may not be authorized by matching numbers alone — the stale refusal
        stands until an explicit rebase rebuilds it."""

        from tests.test_v3_n3_vertical_trace import (
            CapturingTransport, _runtime, _executor, _drive, _request_for,
        )
        from tests.test_v3_n1_root_lifecycle import Candidate

        service = MemoryService()
        service.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer", "A2 cold answer"])
        runtime = _runtime(transport)
        executor = _executor(service, runtime)
        try:
            _drive(executor, _request_for(service, ()))
            session = runtime.endpoint_projection_session("pal:resident")
            self.assertEqual(len(session.chunks), 1,
                             "the first round materialized the lineage")

            root = service.history_root
            # A left replacement the session never consumes (manual commit,
            # no rebase driven).
            root.promote(include_active=True)
            root.begin_compact("manual", reason="test")
            root.mark_ready("manual", Candidate())
            root.commit("manual")
            self.assertEqual(root.left_generation, 1)
            service.append_l1_user("T", user("Q2", "q2"))

            request = _request_for(service, ())
            self.assertIsNone(
                executor._prepare_turn_projection(runtime, request),
                "a materialized stale prefix must keep the strict refusal; "
                "the counter can never authorize retired wire content")
            kinds = [d.get("kind") for d in executor.state.diagnostics]
            self.assertIn("two_segment_turn_projection_stale_left", kinds)
            # The refused round still completes cold (no retired replay, no
            # install): the ordinary path is not blocked by the refusal.
            outcome = _drive(executor, request)
            self.assertEqual(str(getattr(outcome.status, "value", outcome.status)), "ok")

            # An explicit rebase rebuilds the lineage; prepare engages again.
            executor._rebase_projection_after_left_install(
                runtime, "pal:resident", root)
            self.assertIsNotNone(
                executor._prepare_turn_projection(runtime, request))
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
