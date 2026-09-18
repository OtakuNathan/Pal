"""Construction-rule tests for pal.llm.projection_contracts.

PLAN P1 gate: illegal states must be rejected at construction, not by
scattered caller checks.  These tests pin the §4.2 forbidden-combination
matrix and the §5.1 append-proof negatives.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from pal.llm.ir import WireShape
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    ClosedRound,
    DraftRound,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    NativeContinuation,
    NativeContinuationKind,
    NativeMaterial,
    OwnerFence,
    PreparedRequest,
    ProjectionContractError,
    ProjectionIdentity,
    ToolCallRecord,
    ToolOutcome,
    ToolResultRecord,
)


def _binding() -> EndpointBinding:
    return EndpointBinding(
        endpoint_id="deepseek-v4.1-flash",
        model_id="deepseek-flash",
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint="fp-1",
    )


def _attempt() -> AttemptKey:
    return AttemptKey(
        identity=ProjectionIdentity(
            session=LogicalSessionId("pal:resident"),
            binding=_binding(),
            projection_generation=0,
        ),
        owner_fence=OwnerFence(0),
        attempt_id="llm_attempt_1",
    )


def _cursor(epoch: int = 0, seq: int = 3, digest: str = "d" * 64) -> HistoryCursor:
    return HistoryCursor(
        history_epoch=epoch, block_sequence=seq, prefix_digest=digest
    )


class IdentityContractTests(unittest.TestCase):
    def test_empty_scope_rejected(self) -> None:
        with self.assertRaises(ProjectionContractError):
            LogicalSessionId("  ")

    def test_binding_requires_every_field(self) -> None:
        base = dict(
            endpoint_id="e",
            model_id="m",
            wire_shape=WireShape.OPENAI_RESPONSE,
            endpoint_spec_revision="r",
            continuation_policy_version="p",
            config_fingerprint="f",
        )
        EndpointBinding(**base)
        for key in base:
            broken = dict(base)
            broken[key] = ""
            with self.assertRaises(ProjectionContractError, msg=key):
                EndpointBinding(**broken)

    def test_negative_generation_and_fence_rejected(self) -> None:
        with self.assertRaises(ProjectionContractError):
            ProjectionIdentity(
                session=LogicalSessionId("s"),
                binding=_binding(),
                projection_generation=-1,
            )
        with self.assertRaises(ProjectionContractError):
            OwnerFence(-1)

    def test_attempt_key_requires_id(self) -> None:
        identity = ProjectionIdentity(
            session=LogicalSessionId("s"), binding=_binding(), projection_generation=0
        )
        with self.assertRaises(ProjectionContractError):
            AttemptKey(identity=identity, owner_fence=OwnerFence(0), attempt_id="")


class CursorContractTests(unittest.TestCase):
    def test_initial_cursor_is_valid(self) -> None:
        cursor = HistoryCursor.initial()
        self.assertEqual((cursor.history_epoch, cursor.block_sequence), (0, 0))
        self.assertEqual(len(cursor.prefix_digest), 64)

    def test_negative_rejected_and_digest_required(self) -> None:
        with self.assertRaises(ProjectionContractError):
            HistoryCursor(history_epoch=-1)
        with self.assertRaises(ProjectionContractError):
            HistoryCursor(prefix_digest="")

    def test_valid_append_accepted_and_span_checked(self) -> None:
        before = _cursor(seq=3)
        after = HistoryCursor(
            history_epoch=0, block_sequence=5, prefix_digest="e" * 64
        )
        AppendReceipt(before=before, after=after, block_count=2)

    def test_append_cannot_change_epoch_or_zero_blocks(self) -> None:
        after_same_epoch = HistoryCursor(
            history_epoch=0, block_sequence=4, prefix_digest="e" * 64
        )
        with self.assertRaises(ProjectionContractError):
            AppendReceipt(
                before=_cursor(seq=3),
                after=HistoryCursor(
                    history_epoch=1, block_sequence=4, prefix_digest="e" * 64
                ),
                block_count=1,
            )
        with self.assertRaises(ProjectionContractError):
            AppendReceipt(
                before=_cursor(seq=3), after=after_same_epoch, block_count=0
            )

    def test_same_length_is_not_an_append_proof(self) -> None:
        before = _cursor(seq=3)
        after = HistoryCursor(
            history_epoch=0, block_sequence=4, prefix_digest="e" * 64
        )
        receipt = AppendReceipt(before=before, after=after, block_count=1)
        # Same epoch/sequence but a different digest: not the receipt base.
        lookalike = HistoryCursor(
            history_epoch=0, block_sequence=3, prefix_digest="f" * 64
        )
        with self.assertRaises(ProjectionContractError):
            receipt.verify_against(lookalike)
        receipt.verify_against(before)

    def test_commit_receipt_rejects_duplicates_and_empty_ids(self) -> None:
        receipt_append = AppendReceipt(
            before=_cursor(seq=0, digest="0" * 64),
            after=HistoryCursor(0, 1, "1" * 64),
            block_count=1,
        )
        HistoryCommitReceipt(
            attempt=_attempt(),
            append=receipt_append,
            closed_call_ids=("call-a",),
            native_committed=True,
        )
        with self.assertRaises(ProjectionContractError):
            HistoryCommitReceipt(
                attempt=_attempt(),
                append=receipt_append,
                closed_call_ids=("call-a", "call-a"),
                native_committed=True,
            )
        with self.assertRaises(ProjectionContractError):
            HistoryCommitReceipt(
                attempt=_attempt(),
                append=receipt_append,
                closed_call_ids=(" ",),
                native_committed=False,
            )


class RoundContractTests(unittest.TestCase):
    def _call(self, call_id: str) -> ToolCallRecord:
        return ToolCallRecord(call_id=call_id, name="lookup", arguments_json="{}")

    def test_tool_call_arguments_must_be_object_json(self) -> None:
        ToolCallRecord(call_id="c", name="n", arguments_json='{"q": 1}')
        for bad in ("[]", "null", "{invalid", ""):
            with self.assertRaises(ProjectionContractError, msg=bad):
                ToolCallRecord(call_id="c", name="n", arguments_json=bad)

    def test_draft_subset_and_native_inventory_invariants(self) -> None:
        attempt = _attempt()
        DraftRound(
            attempt=attempt,
            calls=(self._call("a"), self._call("b")),
            started=frozenset({"a"}),
            results=frozenset({"a"}),
            native_calls=frozenset({"a", "b"}),
        )
        # results outside started
        with self.assertRaises(ProjectionContractError):
            DraftRound(
                attempt=attempt,
                calls=(self._call("a"),),
                started=frozenset({"a"}),
                results=frozenset({"a", "ghost"}),
                native_calls=frozenset({"a"}),
            )
        # started outside the call inventory
        with self.assertRaises(ProjectionContractError):
            DraftRound(
                attempt=attempt,
                calls=(self._call("a"),),
                started=frozenset({"a", "b"}),
                results=frozenset(),
                native_calls=frozenset({"a"}),
            )
        # stale native inventory (the stale-replay mutant shape)
        with self.assertRaises(ProjectionContractError):
            DraftRound(
                attempt=attempt,
                calls=(self._call("a"), self._call("b")),
                started=frozenset({"a"}),
                results=frozenset({"a"}),
                native_calls=frozenset({"a"}),
            )

    def test_closed_round_requires_exact_protocol_pairing(self) -> None:
        attempt = _attempt()
        call_a = self._call("call-a")
        call_b = self._call("call-b")
        result_a = ToolResultRecord("call-a", "42", ToolOutcome.SUCCESS)
        continuation = NativeContinuation(NativeContinuationKind.ABSENT)
        ClosedRound(
            attempt=attempt,
            calls=(call_a,),
            results=(result_a,),
            continuation=continuation,
        )
        # extra result
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=attempt,
                calls=(),
                results=(result_a,),
                continuation=continuation,
            )
        # dangling call
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=attempt,
                calls=(call_a,),
                results=(),
                continuation=continuation,
            )
        # unknown outcome cannot close
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=attempt,
                calls=(call_a,),
                results=(ToolResultRecord("call-a", "", ToolOutcome.UNKNOWN),),
                continuation=continuation,
            )
        _ = call_b

    def test_closed_round_native_association_rules(self) -> None:
        attempt = _attempt()
        call_a = self._call("call-a")
        result_a = ToolResultRecord("call-a", "42", ToolOutcome.SUCCESS)
        # Review G3: call-ID claims must be backed by the payload itself —
        # the native payload carries the accepted call (matching name and
        # arguments), not an opaque placeholder.
        material = NativeMaterial(
            origin=attempt,
            call_ids=("call-a",),
            payload_json=json.dumps({"content": [
                {"type": "tool_use", "id": "call-a", "name": "lookup", "input": {}},
            ]}),
        )
        ClosedRound(
            attempt=attempt,
            calls=(call_a,),
            results=(result_a,),
            continuation=NativeContinuation(
                NativeContinuationKind.REQUIRED, material
            ),
        )
        # required without material
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=attempt,
                calls=(call_a,),
                results=(result_a,),
                continuation=NativeContinuation(NativeContinuationKind.REQUIRED),
            )
        # native bound to a different attempt/scope
        other_attempt = AttemptKey(
            identity=attempt.identity,
            owner_fence=OwnerFence(1),
            attempt_id="llm_other",
        )
        foreign = NativeMaterial(
            origin=other_attempt,
            call_ids=("call-a",),
            payload_json="{}",
        )
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=attempt,
                calls=(call_a,),
                results=(result_a,),
                continuation=NativeContinuation(
                    NativeContinuationKind.REQUIRED, foreign
                ),
            )
        # stale native inventory
        stale = NativeMaterial(
            origin=attempt, call_ids=("call-a", "call-b"), payload_json="{}"
        )
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=attempt,
                calls=(call_a,),
                results=(result_a,),
                continuation=NativeContinuation(
                    NativeContinuationKind.REQUIRED, stale
                ),
            )
        # absent cannot carry material
        with self.assertRaises(ProjectionContractError):
            NativeContinuation(NativeContinuationKind.ABSENT, material)
        # optional without material is equally illegal
        with self.assertRaises(ProjectionContractError):
            NativeContinuation(NativeContinuationKind.OPTIONAL)


class PreparedRequestTests(unittest.TestCase):
    def test_build_and_digest_tamper_detection(self) -> None:
        request = PreparedRequest.build(
            attempt=_attempt(),
            base_cursor=HistoryCursor.initial(),
            payload={"input": [{"role": "user", "content": "hello"}]},
        )
        self.assertTrue(request.payload_digest)
        self.assertIn("hello", request.payload_json)
        with self.assertRaises(ProjectionContractError):
            PreparedRequest(
                attempt=request.attempt,
                base_cursor=request.base_cursor,
                payload_json=request.payload_json + " ",
                payload_digest=request.payload_digest,
            )

    def test_payload_is_canonical_json(self) -> None:
        request = PreparedRequest.build(
            attempt=_attempt(),
            base_cursor=HistoryCursor.initial(),
            payload={"b": 1, "a": 2},
        )
        self.assertEqual(json.loads(request.payload_json), {"a": 2, "b": 1})

    def test_records_are_frozen(self) -> None:
        cursor = HistoryCursor.initial()
        with self.assertRaises(FrozenInstanceError):
            cursor.block_sequence = 99  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
