"""Red-first tests for the pal_v3_af51d74 review package (F1-F6).

F1 — live projection must encode the EFFECTIVE compiled request against a
      strong binding (resolved endpoint, real spec revision / continuation
      policy version / config fingerprint), never placeholder identity.
F2 — only a typed send receipt (attempt id, resolved binding, applied flag)
      may authorize observe_commit; sync stale-spec refresh must keep the
      projection arguments like the stream path does.
F3 — the captured native candidate must reach the projection owner and be
      committed byte-true when the continuation contract preserves it.
F4 — projection commit happens after the canonical acceptance boundary
      (finalization_only discard/replacement), never merely after provider
      success.
F5 — pending wire tail items carry semantic ownership and die with the
      round that produced them at left replacement.
F6 — soft reset explicitly rolls the HistoryRoot incarnation and retires the
      hosted projection sessions; a lineage whose frozen spans are no longer
      durable must fall back cold instead of replaying retired history.

Executor-level fixtures reuse the N3 vertical-trace harness: real runtime,
invoker, codec decode, MemoryService/HistoryRoot; only frames are faked.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.projection_contracts import (
    AttemptKey, EndpointBinding, HistoryCursor, OwnerFence,
)
from pal.llm.projection_session import EndpointProjectionSession, LeftReplacement
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport import LLMEndpointSpecStaleError
from pal.control.contracts import ControlAction
from pal.core.runtime import PalCore
from pal.memory import MemoryService
from pal.shared import PromptAssemblyContext
from pal.shared.json_values import thaw_json
from tests.test_cache_warm_deadline import _route
from tests.test_v3_n1_root_lifecycle import user
from tests.test_v3_n3_vertical_trace import (
    CapturingTransport, _drive, _executor, _request_for, _runtime,
)


def _msg(role, text, message_id, state=None):
    parts = (TextPartIR(text),)
    kwargs = {"role": role, "parts": parts, "message_id": message_id}
    if state is not None:
        kwargs["state"] = state
    return LLMMessageIR(**kwargs)

from pal.core.turns import LLMRequestEffect
from pal.llm.ir import MessageState, WireShape
from pal.llm.projection_contracts import LogicalSessionId
from pal.llm.projection_session import HistoryView


def _endpoint(endpoint_id="trace-endpoint", model_id="trace-model",
              priority=0) -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id=endpoint_id, provider="openai",
        model_id=model_id, display_name=f"Display {endpoint_id}",
        wire_shape="openai_completion", base_url="https://example.test/v1",
        auth_kind="api_key_ref", credential_ref="key",
        context_window=100_000, max_output_tokens=4_096,
        thinking_levels_blob=["off"], default_thinking_level="off",
        supports_tools=True, supports_streaming=False, supports_vision=False,
        input_modalities_blob=["text"], output_modalities_blob=["text"],
        priority=priority, enabled=True, capabilities_blob={},
    )


class _Settings:
    def __init__(self, endpoint_id="trace-endpoint"):
        self._endpoint_id = endpoint_id

    def get_active_llm_endpoint_id(self):
        return self._endpoint_id

    def get_think_level(self, _endpoint_id):
        return "off"


def _runtime_multi(transport, endpoints, endpoint_id) -> LLMRuntime:
    return LLMRuntime(
        EndpointResolver(endpoints=tuple(endpoints)),
        _Settings(endpoint_id),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport),
        config=SimpleNamespace(
            runtime_root=tempfile.mkdtemp(),
            llm_endpoint_retry_attempts=1,
            llm_max_output_recovery_attempts=0,
        ),
    )


class FallbackTransport:
    """Fails every attempt on the primary endpoint, serves on the secondary."""

    def __init__(self, reply: str, fail_endpoint: str):
        self.reply = reply
        self.fail_endpoint = fail_endpoint
        self.captured = []

    def frames(self, endpoint, transport_request):
        self.captured.append((endpoint.endpoint_id, transport_request))
        if endpoint.endpoint_id == self.fail_endpoint:
            raise RuntimeError("primary endpoint exploded")
        payload = {
            "choices": [{
                "message": {"role": "assistant", "content": self.reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        yield _JSONFrame(0, payload)

    def activate_endpoint(self, endpoint_id):
        pass

    def close(self):
        pass


class StaleOnceTransport(CapturingTransport):
    """Raises LLMEndpointSpecStaleError once, then serves normally."""

    def __init__(self, replies):
        super().__init__(replies)
        self.stale_raised = False

    def frames(self, endpoint, transport_request):
        if not self.stale_raised:
            self.stale_raised = True
            raise LLMEndpointSpecStaleError("endpoint spec stale")
        yield from super().frames(endpoint, transport_request)


class ToolCallTransport(CapturingTransport):
    """Replies with a tool-call assistant message."""

    def frames(self, _endpoint, transport_request):
        self.captured.append(transport_request)
        payload = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "demo", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        yield _JSONFrame(0, payload)


def _continuation(finalization_only: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        budget_failure_feedback_text="",
        llm_round_index=0,
        preferred_llm_endpoint_id=None,
        preferred_llm_model_id=None,
        finalization_only=finalization_only,
        tool_observations=[],
        last_response_mode=None,
        turn_id="T",
    )


def _request_for_policy(memory: MemoryService, max_output: int) -> LLMRequestIR:
    request = _request_for(memory, ())
    return LLMRequestIR(
        messages=request.messages,
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=max_output),
        metadata=dict(request.metadata),
    )


class F1PreparedPlanTests(unittest.TestCase):
    """F1: one immutable prepared-generation plan, then project THAT."""

    def test_plan_compiles_effective_request_with_strong_binding(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        request = _request_for_policy(memory, 8_192)

        plan = runtime.prepare_generation_plan(request)
        self.assertIsNotNone(plan, "runtime must expose a prepared plan")
        # The effective request is the compiled one: model hint resolved and
        # the output cap enforced by the endpoint profile.
        self.assertEqual(plan.effective_request.model_hint, "trace-model")
        self.assertEqual(plan.effective_request.policy.max_output_tokens, 4_096)
        self.assertFalse(plan.compact_required)
        # Strong binding identity: no placeholder spec/policy, a digest
        # fingerprint instead of provider:base_url.
        binding = plan.binding
        self.assertIsInstance(binding, EndpointBinding)
        self.assertNotEqual(binding.endpoint_spec_revision, "spec-1")
        self.assertNotEqual(binding.continuation_policy_version, "policy-1")
        self.assertNotEqual(binding.config_fingerprint, "openai:https://example.test/v1")
        self.assertEqual(binding.endpoint_id, "trace-endpoint")

    def test_session_accepts_plan_binding(self):
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        session = runtime.endpoint_projection_session("pal:resident")
        plan_binding = session.binding

        plan = runtime.prepare_generation_plan(LLMRequestIR(
            messages=(), tools=(),
            policy=GenerationPolicyIR(max_output_tokens=64),
        ))
        session2 = runtime.endpoint_projection_session("pal:resident", plan=plan)
        self.assertIs(session2, session,
                      "same endpoint + same config must keep one lineage")
        self.assertEqual(session2.binding, plan.binding)
        self.assertEqual(plan_binding.endpoint_id, plan.binding.endpoint_id)

    def test_projected_payload_is_the_effective_request_encoding(self):
        """The wire payload actually sent must carry the compiled caps."""
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)

        outcome = _drive(ex, _request_for_policy(memory, 8_192))
        self.assertEqual(
            outcome.status.value if hasattr(outcome.status, "value") else str(outcome.status),
            "ok")
        payload = dict(transport.captured[0].payload)
        self.assertEqual(payload.get("max_tokens"), 4_096,
                         "projection must encode the EFFECTIVE request "
                         "(endpoint cap), not the pre-compile request")


class F2SendReceiptTests(unittest.TestCase):
    """F2: receipt-authorised commit; sync refresh keeps projection args."""

    def test_successful_projected_round_carries_applied_receipt(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)

        outcome = _drive(ex, _request_for(memory, ()))
        receipt = getattr(outcome.payload, "projection_receipt", None)
        self.assertIsNotNone(receipt, "LLMGenerationResult must carry the send receipt")
        self.assertTrue(receipt.applied)
        self.assertEqual(receipt.resolved_endpoint_id, "trace-endpoint")
        session = runtime.endpoint_projection_session("pal:resident")
        self.assertEqual(len(session.chunks), 1)

    def test_fallback_send_does_not_commit_projection(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        primary = _endpoint("trace-endpoint", "trace-model", priority=0)
        secondary = _endpoint("fallback-endpoint", "fallback-model", priority=1)
        transport = FallbackTransport("FALLBACK ANSWER", "trace-endpoint")
        runtime = _runtime_multi(
            transport, (primary, secondary), "trace-endpoint")
        ex = _executor(memory, runtime)

        request = _request_for(memory, ())
        request = LLMRequestIR(
            messages=request.messages, tools=(), policy=request.policy,
            metadata={**dict(request.metadata),
                      "preferred_endpoint_id": "trace-endpoint",
                      "endpoint_fallback_policy": "enabled"},
        )
        outcome = _drive(ex, request)
        self.assertEqual(
            outcome.status.value if hasattr(outcome.status, "value") else str(outcome.status),
            "ok")
        receipt = getattr(outcome.payload, "projection_receipt", None)
        self.assertIsNotNone(receipt)
        self.assertFalse(receipt.applied,
                         "the projection was prepared for the primary endpoint "
                         "but the fallback endpoint actually served")
        self.assertEqual(receipt.resolved_endpoint_id, "fallback-endpoint")
        session = runtime.endpoint_projection_session("pal:resident")
        self.assertEqual(len(session.chunks), 0,
                         "an unapplied projection must never freeze a chunk")

    def test_sync_spec_refresh_keeps_projection(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = StaleOnceTransport(["A1 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)

        outcome = _drive(ex, _request_for(memory, ()))
        self.assertEqual(
            outcome.status.value if hasattr(outcome.status, "value") else str(outcome.status),
            "ok")
        receipt = getattr(outcome.payload, "projection_receipt", None)
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt.applied,
                        "sync stale-spec refresh must keep the projection "
                        "arguments on recursion, like the stream path")
        self.assertEqual(len(transport.captured), 1,
                         "exactly one provider send after the refresh retry")


class F3NativeContinuationTests(unittest.TestCase):
    """F3: captured native material reaches the owner and freezes byte-true."""

    def test_native_candidate_survives_commit_on_projected_round(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)

        outcome = _drive(ex, _request_for(memory, ()))
        receipt = getattr(outcome.payload, "projection_receipt", None)
        self.assertIsNotNone(receipt)
        native = getattr(receipt, "native", None)
        self.assertIsNotNone(native,
                             "a captured, codec-envelope-backed candidate must "
                             "travel with the receipt")
        self.assertIn("A1 answer", native.payload_json)

        session = runtime.endpoint_projection_session("pal:resident")
        chunk = session.chunks[0]
        self.assertEqual(len(session.chunks), 1)
        stored = session.native_for(chunk.round_attempt_id)
        self.assertIsNotNone(stored,
                             "the projection owner must keep the native "
                             "material for the committed attempt")
        self.assertEqual(stored["payload_json"], native.payload_json)
        # Byte-true freeze: the chunk carries the native-derived assistant
        # item (openai_completion wire items ARE the message object).
        items = [thaw_json(item) for item in chunk.items]
        self.assertTrue(any(
            item.get("role") == "assistant" and item.get("content") == "A1 answer"
            for item in items
        ))


class F4AcceptanceBoundaryTests(unittest.TestCase):
    """F4: commit after the canonical acceptance boundary."""

    def test_finalization_discard_is_not_frozen(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = ToolCallTransport(["ignored"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)

        request = _request_for(memory, ())
        executor_ex = ex

        async def run():
            return await executor_ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),
                _continuation(finalization_only=True),
            )

        ex.build_turn_prompt = lambda *args, **kwargs: request
        result = asyncio.run(run())
        outcome = result.payload
        # Canonical L1 replaced the tool-call answer with fallback text.
        self.assertFalse(outcome.tool_calls)

        session = runtime.endpoint_projection_session("pal:resident")
        self.assertEqual(len(session.chunks), 1)
        span = session.chunks[0].semantic_span
        assistants = [m for t in memory.l1_store.turns.turns
                      for m in t.messages if m.role == MessageRole.ASSISTANT]
        self.assertTrue(assistants, "fallback text must be accepted into L1")
        self.assertIn(assistants[0].message_id, span,
                      "the chunk must freeze the FINAL accepted contribution")
        discarded = [m for m in assistants if m.tool_calls] if hasattr(assistants[0], "tool_calls") else []
        for message in memory.l1_store.turns.turns[0].messages:
            if message.role == MessageRole.ASSISTANT and message.message_id not in span:
                self.fail("a discarded assistant message id froze into the chunk")


class F5PendingTailOwnershipTests(unittest.TestCase):
    """F5: pending wire tail dies with the round that produced it."""

    def _session(self) -> EndpointProjectionSession:
        session = EndpointProjectionSession(LogicalSessionId("s1"))
        session.bind(_endpoint_binding(WireShape.ANTHROPIC_MESSAGES))
        return session

    def _commit(self, session, attempt, messages, span_ids):
        session.begin_round(attempt, requires_native=False)
        session.prepare_normal(
            HistoryView(cursor=session.frontier, messages=messages))
        session.observe_commit(
            _commit_receipt(attempt, session, span_ids=span_ids),
            accepted_messages=(),
            span_message_ids=span_ids,
        )

    def test_pending_tail_items_die_with_retired_round(self):
        session = self._session()
        # Round 1: ends with a trailing user message; Anthropic trims it into
        # the pending tail instead of freezing it (span S1 = round 1).
        self._commit(session, _attempt_for(session, "att-1"), (
            _msg(MessageRole.USER, "TOOL-RESULT-PENDING", "m-user-1"),
            _msg(MessageRole.ASSISTANT, "assistant answer", "m-assist-1",
                 MessageState.COMPLETE),
            _msg(MessageRole.USER, "TRIMMED-BY-MERGE", "m-user-2"),
        ), ("m-user-1", "m-assist-1", "m-user-2"))
        self.assertTrue(session._pending_wire_tail,
                        "anthropic trailing user items must sit in the pending tail")

        # Round 2 (surviving right side) commits past the pending bytes.
        self._commit(session, _attempt_for(session, "att-2"), (
            _msg(MessageRole.USER, "right-side", "m-user-3"),
            _msg(MessageRole.ASSISTANT, "right answer", "m-assist-2",
                 MessageState.COMPLETE),
        ), ("m-user-3", "m-assist-2"))

        # Left replacement: only round 2 survives; round 1 belongs to the
        # compacted-away L.
        session.on_left_replaced(LeftReplacement(
            seed_messages=(_msg(MessageRole.USER, "SUMMARY", "m-summary"),),
            kept_frozen_messages=(
                _msg(MessageRole.USER, "right-side", "m-user-3"),
                _msg(MessageRole.ASSISTANT, "right answer", "m-assist-2",
                     MessageState.COMPLETE),
            ),
            cursor_after=HistoryCursor(history_epoch=1, block_sequence=1,
                                       prefix_digest="1" * 64),
            left_revision=1,
        ))
        session.begin_round(_attempt_for(session, "att-3"), requires_native=False)
        prepared3 = session.prepare_normal(HistoryView(
            cursor=session.frontier,
            messages=(_msg(MessageRole.USER, "fresh tail", "m-user-4"),),
        ))
        blob = prepared3.payload_json
        self.assertNotIn("TRIMMED-BY-MERGE", blob,
                         "pending wire bytes from the retired round must die "
                         "with it, not reappear after the summary")
        self.assertNotIn("TOOL-RESULT-PENDING", blob)
        self.assertIn("SUMMARY", blob)
        self.assertIn("right answer", blob,
                      "surviving right-side material stays frozen")
        self.assertIn("fresh tail", blob)


class F6ResetLineageTests(unittest.TestCase):
    """F6: explicit reset rolls incarnation + retires projection lineage."""

    def test_soft_reset_rolls_root_incarnation(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        before = memory.history_root.incarnation
        memory.soft_reset()
        root = memory.history_root
        self.assertNotEqual(root.incarnation, before,
                            "soft_reset must roll the session incarnation")
        self.assertEqual(root.left_generation, 0)
        self.assertEqual(len(memory.l1_store.turns.turns), 0)

    def test_retire_projection_sessions(self):
        transport = CapturingTransport(["A1 answer"])
        runtime = _runtime(transport)
        session = runtime.endpoint_projection_session("pal:resident")
        self.assertFalse(session.retired)
        runtime.retire_projection_sessions()
        self.assertTrue(session.retired,
                        "reset must retire the hosted projection sessions")
        fresh = runtime.endpoint_projection_session("pal:resident")
        self.assertIsNot(fresh, session)
        self.assertFalse(fresh.retired)
        self.assertEqual(fresh.identity.projection_generation, 0)

    def test_nondurable_frozen_spans_fall_back_cold_after_soft_reset(self):
        memory = MemoryService()
        memory.begin_l1_turn("T", user_message=user("Q1", "q1"))
        transport = CapturingTransport(["A1 answer", "A2 answer"])
        runtime = _runtime(transport)
        ex = _executor(memory, runtime)
        _drive(ex, _request_for(memory, ()))

        # A direct service-level reset (no runtime retirement wired) must
        # still leave the lineage unusable: the frozen chunk spans reference
        # history that no longer exists.
        memory.soft_reset()
        memory.begin_l1_turn("T2", user_message=user("Q2", "q2"))
        request2 = _request_for(memory, ())
        pack = ex._prepare_turn_projection(runtime, request2)
        self.assertIsNone(pack,
                          "a lineage whose frozen spans are not durable must "
                          "not replay retired history")
        kinds = [d.get("kind") for d in ex.state.diagnostics]
        self.assertIn("two_segment_turn_projection_lineage_stale", kinds)


def _endpoint_binding(shape) -> EndpointBinding:
    return EndpointBinding(
        endpoint_id="endpoint-1",
        model_id="model-1",
        wire_shape=shape,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint="fp-1",
    )


def _attempt_for(session: EndpointProjectionSession, attempt_id: str) -> AttemptKey:
    return AttemptKey(
        identity=session.identity,
        owner_fence=OwnerFence(0),
        attempt_id=attempt_id,
    )


def _commit_receipt(attempt: AttemptKey, session: EndpointProjectionSession,
                    *, span_ids) -> "object":
    import hashlib

    from pal.llm.projection_contracts import AppendReceipt, HistoryCommitReceipt

    before = session.frontier
    after = HistoryCursor(
        history_epoch=before.history_epoch,
        block_sequence=before.block_sequence + 1,
        prefix_digest=hashlib.sha256(
            "".join(span_ids).encode("utf-8")).hexdigest(),
    )
    return HistoryCommitReceipt(
        attempt=attempt,
        append=AppendReceipt(before=before, after=after, block_count=1),
        closed_call_ids=(),
        native_committed=False,
    )


class OwnerPortWiringTests(unittest.TestCase):
    """Runtime control paths reach owners through their ports (G3c-2/F6).

    The two-segment interrupt/reset branches and the F6 lineage retirement
    were written against a ``context.get_port`` that never existed on
    MainContext; while full_source was the default they stayed unreachable,
    so the wiring was never proven against a real context — /interrupt
    crashed and the reset-side protections silently no-oped the moment
    two_segment became reachable.
    """

    def test_interrupt_reaches_memory_owner(self):
        core = PalCore()
        core.context.port_registry["memory:memory"] = MemoryService()
        replies: list[str] = []

        async def record(action, text):
            replies.append(str(text))

        core._complete_action_reply_async = record
        asyncio.run(
            core._handle_interrupt_turn_async(
                ControlAction(
                    action_kind="interrupt",
                    target_scope="memory",
                    route=_route(),
                )
            )
        )
        self.assertEqual(replies, ["No active turn to interrupt."])

    def test_execute_soft_reset_retires_projection_sessions_via_port(self):
        core = PalCore()
        transport = CapturingTransport(["unused"])
        llm = _runtime(transport)
        session = llm.endpoint_projection_session("pal:resident")
        self.assertFalse(session.retired)
        core.context.port_registry["memory:memory"] = MemoryService()
        core.context.port_registry["llm:llm"] = llm
        applied = asyncio.run(core._execute_soft_reset_async(SimpleNamespace()))
        self.assertTrue(applied)
        self.assertTrue(
            session.retired,
            "soft reset must retire hosted projection sessions via the llm port",
        )


if __name__ == "__main__":
    unittest.main()
