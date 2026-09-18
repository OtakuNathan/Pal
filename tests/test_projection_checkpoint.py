"""P4: projection checkpoint atomicity and invoker native-capture wiring."""
from __future__ import annotations

import json
import unittest

from pal.llm.ir import MessageRole, TextPartIR, WireShape
from pal.llm.projection_checkpoint import (
    PROJECTION_CHECKPOINT_SCHEMA_VERSION,
    ProjectionCheckpointError,
    snapshot_projection,
    restore_projection,
)
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    OwnerFence,
    ToolCallRecord,
    ToolOutcome,
    ToolResultRecord,
    ClosedRound,
    NativeContinuation,
    NativeContinuationKind,
    NativeMaterial,
)
from pal.llm.projection_session import EndpointProjectionSession, HistoryView
from pal.llm.continuation_policy import NativeCandidate


def _binding(suffix: str = "1") -> EndpointBinding:
    return EndpointBinding(
        endpoint_id=f"e-{suffix}",
        model_id=f"m-{suffix}",
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint=f"fp-{suffix}",
    )


def _attempt(session: EndpointProjectionSession, attempt_id: str) -> AttemptKey:
    return AttemptKey(
        identity=session.identity,  # type: ignore[arg-type]
        owner_fence=OwnerFence(0),
        attempt_id=attempt_id,
    )


def _user(text: str):
    from pal.llm.ir import LLMMessageIR

    return LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),))


def _committed_session() -> EndpointProjectionSession:
    session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
    session.bind(_binding())
    session.begin_round(_attempt(session, "a1"), requires_native=True)
    # Native attaches to the OPEN round before the commit (review R4/R7):
    # post-commit attachments from closed rounds are refused.
    session.attach_native(
        _attempt(session, "a1"),
        NativeCandidate(
            wire_shape=WireShape.OPENAI_COMPLETION,
            endpoint_id="e-1",
            model_id="m-1",
            payload_json='{"message": {"role": "assistant", "reasoning_content": "x"}}',
            call_ids=(),
        ),
    )
    session.prepare(
        HistoryView(cursor=HistoryCursor.initial(), messages=(_user("q"),))
    )
    session.observe_commit(
        HistoryCommitReceipt(
            attempt=_attempt(session, "a1"),
            append=AppendReceipt(
                before=HistoryCursor.initial(),
                after=HistoryCursor(0, 1, "d" * 64),
                block_count=1,
            ),
            closed_call_ids=(),
            native_committed=True,
        )
    )
    return session


class ProjectionCheckpointTests(unittest.TestCase):
    def test_roundtrip_restores_lineage(self) -> None:
        original = _committed_session()
        payload = {"projection": snapshot_projection(original)}

        restored = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        bound = restore_projection(
            payload, l1_history_cursor=HistoryCursor(0, 1, "d" * 64), session=restored
        )
        self.assertTrue(bound)
        self.assertEqual(restored.identity, original.identity)
        self.assertEqual(restored.frontier, original.frontier)
        self.assertEqual(
            sorted(restored.native_by_attempt), sorted(original.native_by_attempt)
        )

    def test_frontier_ahead_of_l1_is_refused(self) -> None:
        original = _committed_session()
        payload = {"projection": snapshot_projection(original)}
        session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        with self.assertRaisesRegex(ProjectionCheckpointError, "mismatched"):
            restore_projection(
                payload,
                l1_history_cursor=HistoryCursor(0, 0, "0" * 64),  # older IR
                session=session,
            )

    def test_schema_mismatch_and_scope_mismatch_refused(self) -> None:
        original = _committed_session()
        bad_schema = {"projection": dict(snapshot_projection(original), schema_version="ancient")}
        wrong_scope = {"projection": dict(snapshot_projection(original), scope="other")}
        session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        with self.assertRaisesRegex(ProjectionCheckpointError, "schema"):
            restore_projection(bad_schema, l1_history_cursor=HistoryCursor(0, 1, "d" * 64), session=session)
        with self.assertRaisesRegex(ProjectionCheckpointError, "scope"):
            restore_projection(wrong_scope, l1_history_cursor=HistoryCursor(0, 1, "d" * 64), session=session)

    def test_truncated_duplicate_and_invalid_native_records_refused(self) -> None:
        original = _committed_session()
        base = snapshot_projection(original)
        for mutate in (
            lambda p: p["native_records"].append({"attempt_id": "x2"}),  # no payload
            lambda p: p["native_records"].append(dict(p["native_records"][0])),  # duplicate
            lambda p: p["native_records"].__setitem__(
                0, dict(p["native_records"][0], payload_json="{invalid")
            ),
        ):
            broken = json.loads(json.dumps(base))
            mutate(broken)
            session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
            with self.assertRaises(ProjectionCheckpointError):
                restore_projection(
                    {"projection": broken},
                    l1_history_cursor=HistoryCursor(0, 1, "d" * 64),
                    session=session,
                )

    def test_legacy_snapshot_starts_fresh_lineage(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        bound = restore_projection(
            {}, l1_history_cursor=HistoryCursor.initial(), session=session
        )
        self.assertFalse(bound)
        self.assertIsNone(session.identity)

    def test_unbound_snapshot_round_trips(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("s"))
        payload = snapshot_projection(session)
        self.assertFalse(payload["bound"])
        restored = EndpointProjectionSession(LogicalSessionId("s"))
        self.assertFalse(
            restore_projection(
                {"projection": payload},
                l1_history_cursor=HistoryCursor.initial(),
                session=restored,
            )
        )


class InvokerNativeCaptureTests(unittest.TestCase):
    def test_attempt_result_carries_native_payload(self) -> None:
        from pal.llm.endpoint import ShapeEndpointInvoker
        from pal.llm.models import LLMEndpointModel
        from pal.llm.ir import GenerationPolicyIR, LLMRequestIR

        frames = [
            {
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "sig-1"},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "stop",
                "usage": {"input_tokens": 3, "output_tokens": 5},
            }
        ]

        class _FramesTransport:
            def frames(self, request):
                for index, payload in enumerate(frames):
                    from pal.llm.shapes.base import _JSONFrame

                    yield _JSONFrame(index, payload)

            def activate_endpoint(self, endpoint_id: str) -> None:
                pass

            def close(self) -> None:
                pass

        attempts: list = []
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(),  # type: ignore[arg-type]
            attempt_sink=attempts.append,
        )
        endpoint = LLMEndpointModel(
            endpoint_id="anthropic-1",
            provider="anthropic",
            model_id="claude-x",
            base_url="",
            auth_kind="api_key_ref",
            credential_ref="key",
            context_window=10_000,
            max_output_tokens=1_000,
            thinking_levels_blob=[],
            default_thinking_level="",
            supports_tools=True,
            supports_streaming=False,
            supports_vision=False,
            input_modalities_blob=["text"],
            output_modalities_blob=["text"],
            priority=0,
            enabled=True,
            capabilities_blob={},
            wire_shape="anthropic_messages",
        )
        request = LLMRequestIR(
            messages=(_user("hello"),),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=64),
        )
        response, _updates = invoker.invoke(endpoint, request)

        self.assertEqual(len(attempts), 1)
        native_json = attempts[0].native_payload_json
        self.assertTrue(native_json)
        payload = json.loads(native_json)
        self.assertEqual(payload["content"][0]["signature"], "sig-1")
        # Semantics unaffected by the capture wiring.
        self.assertEqual(response.text, "answer")


if __name__ == "__main__":
    unittest.main()
