"""Multi-session isolation on the shared projection implementation.

Bunshin coder/verifier/resident share the projection class, while session state
stays isolated. Tests cover scope separation, role retirement and wire parity."""
from __future__ import annotations

import json
import unittest

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, WireShape
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    OwnerFence,
)
from pal.llm.projection_session import EndpointProjectionSession, HistoryView, ProjectionSessionError


def _binding() -> EndpointBinding:
    # SAME binding object value for every role: same endpoint, same model.
    return EndpointBinding(
        endpoint_id="deepseek-v4.1-flash",
        model_id="deepseek-flash",
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint="fp-1",
    )


def _attempt(session: EndpointProjectionSession, attempt_id: str, fence: int = 0) -> AttemptKey:
    return AttemptKey(
        identity=session.identity,  # type: ignore[arg-type]
        owner_fence=OwnerFence(fence),
        attempt_id=attempt_id,
    )


def _user(text: str) -> LLMMessageIR:
    return LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),))


def _assistant(text: str) -> LLMMessageIR:
    return LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR(text),))


def _commit(session: EndpointProjectionSession, attempt_id: str, *, before: HistoryCursor, after: HistoryCursor, call: str = "call-a") -> HistoryCommitReceipt:
    receipt = HistoryCommitReceipt(
        attempt=_attempt(session, attempt_id),
        append=AppendReceipt(before=before, after=after, block_count=1),
        closed_call_ids=(call,),
        native_committed=False,
    )
    session.observe_commit(receipt)
    return receipt


def _driven_session(scope: str, attempt_id: str, call: str = "call-a"):
    session = EndpointProjectionSession(LogicalSessionId(scope))
    session.bind(_binding())
    session.begin_round(_attempt(session, attempt_id), requires_native=False)
    session.prepare(HistoryView(cursor=HistoryCursor.initial(), messages=(_user("q"), _assistant("a"))))
    receipt = _commit(
        session, attempt_id, before=HistoryCursor.initial(), after=HistoryCursor(0, 1, "d" * 64), call=call
    )
    return session, receipt


class ScopeIsolationTests(unittest.TestCase):
    def test_same_endpoint_same_call_ids_do_not_cross(self) -> None:
        coder, coder_receipt = _driven_session("bunshin:coder:run-1", "a1", call="call-a")
        verifier, verifier_receipt = _driven_session("bunshin:verifier:run-1", "a1", call="call-a")

        # Identical inputs, completely independent state.
        self.assertEqual(len(coder.chunks), 1)
        self.assertEqual(len(verifier.chunks), 1)
        self.assertIsNot(coder.chunks[0], verifier.chunks[0])
        self.assertEqual(coder.frontier, verifier.frontier)

        # Independent continuation: coder advances, verifier untouched.
        coder.begin_round(_attempt(coder, "a2"), requires_native=False)
        coder.prepare(HistoryView(cursor=coder.frontier, messages=(_user("q2"), _assistant("a2"))))
        _commit(coder, "a2", before=HistoryCursor(0, 1, "d" * 64), after=HistoryCursor(0, 2, "e" * 64))
        self.assertEqual(len(coder.chunks), 2)
        self.assertEqual(len(verifier.chunks), 1)
        self.assertEqual(verifier.frontier, HistoryCursor(0, 1, "d" * 64))

        # A verifier receipt cannot be replayed into the coder session:
        # different identity (scope) makes the attempt foreign.
        with self.assertRaises(ProjectionSessionError):
            coder.observe_commit(verifier_receipt)
        _ = coder_receipt

    def test_retired_session_releases_everything(self) -> None:
        session, _ = _driven_session("bunshin:coder:run-2", "a1")
        session.retire()
        self.assertEqual(session.chunks, ())
        self.assertEqual(session.native_by_attempt, {})
        with self.assertRaises(ProjectionSessionError):
            session.begin_round(
                AttemptKey(
                    identity=session.identity,  # type: ignore[arg-type]
                    owner_fence=OwnerFence(0),
                    attempt_id="later",
                ),
                requires_native=False,
            )


class SessionLayerParityTests(unittest.TestCase):
    def test_direct_and_proxy_roles_produce_identical_payloads(self) -> None:
        resident, _ = _driven_session("pal:resident", "a1")
        coder, _ = _driven_session("bunshin:coder:run-5", "a1")

        resident.begin_round(_attempt(resident, "a2"), requires_native=False)
        coder.begin_round(_attempt(coder, "a2"), requires_native=False)
        view = HistoryView(cursor=resident.frontier, messages=(_user("q2"), _assistant("a2")))

        direct_items = json.loads(resident.prepare(view).payload_json)["messages"]
        proxy_items = json.loads(coder.prepare(view).payload_json)["messages"]
        # Same shared implementation, same binding: byte-identical assembly.
        # Transport differences never reach the session layer.
        self.assertEqual(direct_items, proxy_items)


if __name__ == "__main__":
    unittest.main()
