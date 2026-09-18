"""Session-level endpoint projection owner (PLAN §3/P3).

One EndpointProjectionSession per logical conversation; single writer.  It
owns:

- the active EndpointBinding and its projection generation,
- the frontier cursor, advanced ONLY by trusted HistoryCommitReceipts,
- immutable projection chunks carved from requests that were actually sent
  (a chunk is the wire items beyond the previous frontier in the last
  prepared request of the round — byte-true by construction),
- the NativeContinuationStore for the active binding (destroyed on switch).

prepare() never re-normalizes; repair outcomes arrive as repaired
ClosedRounds from the shared runtime.  The fast path encodes only the tail
after the frontier; a generation change forces one full rebuild.  When the
required-native contract cannot be satisfied, prepare fails explicitly —
no silent semantic-only fallback (PLAN §0.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pal.llm.continuation_policy import (
    ContinuationDecisionKind,
    NativeCandidate,
    validate_candidate,
)
from pal.llm.ir import LLMMessageIR
from pal.llm.projection_contracts import (
    AttemptKey,
    ClosedRound,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    NativeContinuationKind,
    PreparedRequest,
    ProjectionContractError,
    ProjectionIdentity,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext

__all__ = [
    "ProjectionChunk",
    "ProjectionSessionError",
    "ContinuationUnavailable",
    "EndpointProjectionSession",
    "HistoryView",
]


class ProjectionSessionError(RuntimeError):
    """The projection session refused an operation (state mismatch)."""


class ContinuationUnavailable(ProjectionSessionError):
    """Required native continuation is missing or degraded; explicit stop."""


_ITEM_CONTAINER_KEY = {
    "openai_response": "input",
    "openai_completion": "messages",
    "anthropic_messages": "messages",
}


@dataclass(frozen=True)
class HistoryView:
    """What the L1 writer shows the session for one prepare call.

    ``cursor`` is the base of this view and must equal the session frontier
    (append-proof continuity).  ``messages`` is everything NOT yet frozen
    into committed chunks — for a fast-path prepare that is the tail after
    the frontier; after a generation switch it is the full history.
    """

    cursor: HistoryCursor
    messages: tuple[LLMMessageIR, ...]
    epoch_note: str = ""


@dataclass(frozen=True)
class ProjectionChunk:
    """Immutable wire items for one committed round, carved from a sent request."""

    round_attempt_id: str
    cursor_before: HistoryCursor
    cursor_after: HistoryCursor
    items: tuple[dict, ...]
    prefix_digest: str


@dataclass
class _ActiveRound:
    attempt: AttemptKey
    requires_native: bool
    prepared_items: list[dict] = field(default_factory=list)
    prepared_base_cursor: HistoryCursor | None = None


class EndpointProjectionSession:
    """Single-writer projection owner for one logical session."""

    def __init__(self, session_id: LogicalSessionId) -> None:
        self.session_id = session_id
        self.binding: EndpointBinding | None = None
        self.identity: ProjectionIdentity | None = None
        self.frontier: HistoryCursor = HistoryCursor.initial()
        self.chunks: tuple[ProjectionChunk, ...] = ()
        self.native_by_attempt: dict[str, dict] = {}
        self._committed_attempts: dict[str, HistoryCommitReceipt] = {}
        self._frontier_item_count: int = 0
        self._active: _ActiveRound | None = None
        # Amortized assembled prefix of all frozen chunk items; extended on
        # commit only, never rebuilt per prepare (PLAN §11: no per-round
        # deepcopy of old chunks).
        self._prefix_items: list[dict] = []
        self.retired = False

    # -- binding lifecycle -------------------------------------------------

    def bind(self, binding: EndpointBinding) -> None:
        """Initial bind or rebind after switch; destroys the old lineage."""

        if self.retired:
            raise ProjectionSessionError("session is retired")
        generation = 0 if self.identity is None else self.identity.projection_generation + 1
        self.binding = binding
        self.identity = ProjectionIdentity(
            session=self.session_id,
            binding=binding,
            projection_generation=generation,
        )
        # Endpoint switch destroys old active data (PLAN §8.1): native store,
        # derived chunks, frontier coverage, and any open round die together.
        self.native_by_attempt = {}
        self.chunks = ()
        self._frontier_item_count = 0
        self._committed_attempts = {}
        self.frontier = HistoryCursor.initial()
        self._prefix_items = []
        self._active = None

    def retire(self) -> None:
        self.retired = True
        self.native_by_attempt = {}
        self.chunks = ()
        self._prefix_items = []
        self._active = None

    def _require_identity(self) -> ProjectionIdentity:
        if self.identity is None or self.binding is None:
            raise ProjectionSessionError("session is not bound to an endpoint")
        if self.retired:
            raise ProjectionSessionError("session is retired")
        return self.identity

    # -- native continuation -----------------------------------------------

    def attach_native(self, attempt: AttemptKey, candidate: NativeCandidate) -> None:
        """Validate and store native material for an attempt.

        Degraded/unsupported REQUIRED material raises ContinuationUnavailable:
        the round cannot legally continue on this protocol, and silently
        re-encoding from IR is forbidden (PLAN §0.2/§6.3).
        """

        self._require_identity()
        if attempt.identity != self.identity:
            raise ProjectionSessionError("attempt belongs to a different projection identity")
        decision = validate_candidate(candidate)
        if decision.kind is not ContinuationDecisionKind.PRESERVED:
            detail = "; ".join(f"{issue.code}: {issue.detail}" for issue in decision.issues)
            raise ContinuationUnavailable(
                f"native continuation for {attempt.attempt_id} is {decision.kind.value}: {detail}"
            )
        self.native_by_attempt[attempt.attempt_id] = {
            "payload_json": candidate.payload_json,
            "call_ids": list(candidate.call_ids),
            "endpoint_id": candidate.endpoint_id,
            "model_id": candidate.model_id,
        }

    def native_for(self, attempt_id: str) -> dict | None:
        return self.native_by_attempt.get(attempt_id)

    # -- rounds and commits --------------------------------------------------

    def begin_round(self, attempt: AttemptKey, *, requires_native: bool) -> None:
        identity = self._require_identity()
        if self._active is not None:
            raise ProjectionSessionError("a round is already open for this session")
        if attempt.identity != identity:
            raise ProjectionSessionError("attempt identity does not match the session binding")
        self._active = _ActiveRound(attempt=attempt, requires_native=requires_native)

    def close_round(self) -> AttemptKey:
        """Close the in-flight round without committing (cancel path)."""

        if self._active is None:
            raise ProjectionSessionError("no open round")
        attempt = self._active.attempt
        self._active = None
        return attempt

    def observe_commit(self, receipt: HistoryCommitReceipt) -> None:
        """Advance the frontier with a trusted joint commit (idempotent)."""

        self._require_identity()
        previous = self._committed_attempts.get(receipt.attempt.attempt_id)
        if previous is not None:
            # Idempotency first: a replayed receipt for an already-committed
            # attempt is a no-op even though the round has since closed.
            if previous == receipt:
                return
            raise ProjectionSessionError("conflicting receipts for one attempt")
        if self._active is None or receipt.attempt != self._active.attempt:
            raise ProjectionSessionError("commit receipt does not match the open round")
        receipt.append.verify_against(self.frontier)
        if receipt.append.after.history_epoch != self.frontier.history_epoch:
            raise ProjectionSessionError("commit receipt epoch does not match frontier")
        items = tuple(self._active.prepared_items[self._frontier_item_count :])
        if not items:
            raise ProjectionSessionError(
                "no prepared items beyond the frontier; commit has nothing to seal"
            )
        # Anthropic merges adjacent user-role wire messages, so a wire item
        # ending in role "user" is not a prefix-stable freeze point: the next
        # request's encoder would merge it with the following user message.
        # Trim trailing mergeable items back into the unfrozen tail; they are
        # re-encoded (bounded cost) until a later commit freezes past them.
        frozen_item_count = len(self._active.prepared_items)
        if self.binding.wire_shape.value == "anthropic_messages":
            while items and isinstance(items[-1], dict) and items[-1].get("role") == "user":
                items = items[:-1]
                frozen_item_count -= 1
        if not items:
            raise ProjectionSessionError(
                "round produced no prefix-stable items to freeze"
            )
        chunk = ProjectionChunk(
            round_attempt_id=receipt.attempt.attempt_id,
            cursor_before=self.frontier,
            cursor_after=receipt.append.after,
            items=items,
            prefix_digest=receipt.append.after.prefix_digest,
        )
        self.chunks = (*self.chunks, chunk)
        self._prefix_items.extend(items)
        self._committed_attempts[receipt.attempt.attempt_id] = receipt
        self.frontier = receipt.append.after
        self._frontier_item_count = frozen_item_count
        if not receipt.native_committed:
            # A commit that carried no native while the binding requires it
            # leaves the lineage unsealable for same-endpoint replay; the
            # chunk stays (derived), the native gap is explicit.
            self.native_by_attempt.pop(receipt.attempt.attempt_id, None)
        self._active = None

    def reject_commit(self, attempt_id: str, reason: str) -> None:
        """Drop an open round without sealing (late/failed response)."""

        _ = reason
        if self._active is None or self._active.attempt.attempt_id != attempt_id:
            raise ProjectionSessionError("no matching open round to reject")
        self._active = None

    # -- preparation ---------------------------------------------------------

    def prepare(self, view: HistoryView, *, controls: dict | None = None) -> PreparedRequest:
        """Build the next request: frozen chunks + newly encoded tail.

        Fast path requires the view cursor to equal the frontier (the L1
        writer and the session agree through the receipt chain).  A cursor
        mismatch is an explicit failure — same length is not an append proof.
        """

        identity = self._require_identity()
        if self._active is None:
            raise ProjectionSessionError("prepare requires an open round (begin_round first)")
        if view.cursor != self.frontier:
            raise ProjectionSessionError(
                "history view cursor does not match the projection frontier; "
                "append proof missing"
            )
        codec = codec_for_shape(self.binding.wire_shape)
        context = ShapeContext(
            wire_shape=self.binding.wire_shape,
            endpoint_id=self.binding.endpoint_id,
            model_id=self.binding.model_id,
        )
        # Frozen prefix comes from the amortized cache: no per-item copies,
        # no re-encode of committed items.
        items: list[dict] = list(self._prefix_items)
        # Tail: messages after the committed prefix.  With no receipts yet
        # the whole view is the tail (full encode once per generation).
        from pal.llm.ir import GenerationPolicyIR, LLMRequestIR

        request = LLMRequestIR(
            messages=view.messages,
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        encoded = codec.encode(request, context)
        container = _ITEM_CONTAINER_KEY.get(self.binding.wire_shape.value, "input")
        tail_items = list(encoded.payload.get(container) or [])
        items.extend(dict(item) for item in tail_items)
        self._active.prepared_items = items
        self._active.prepared_base_cursor = view.cursor
        # Items are plain dicts by construction (codec encode output); no
        # defensive deep conversion — it dominated prepare cost (measured
        # 9.9ms of 17.4ms at n=800) with zero effect.
        payload = {
            container: items,
            **({"controls": dict(controls)} if controls else {}),
        }
        return PreparedRequest.build(
            attempt=self._active.attempt,
            base_cursor=view.cursor,
            payload=payload,
        )

    # -- repair ---------------------------------------------------------------

    def accept_repaired_round(self, closed: ClosedRound, *, cursor_after: HistoryCursor, block_count: int) -> HistoryCommitReceipt:
        """Seal a repaired round produced by the shared runtime.

        The session never repairs by itself; it only accepts runtime-validated
        repaired rounds (dangling calls pruned, native association intact).
        """

        from pal.llm.projection_contracts import AppendReceipt

        if self._active is None or closed.attempt != self._active.attempt:
            raise ProjectionSessionError("repaired round does not match the open round")
        if closed.continuation.kind is NativeContinuationKind.REQUIRED:
            material = closed.continuation.material
            if material is None:
                raise ContinuationUnavailable("repaired round lost required native material")
            self.attach_native(closed.attempt, _candidate_from(material))
        receipt = HistoryCommitReceipt(
            attempt=closed.attempt,
            append=AppendReceipt(
                before=self.frontier,
                after=cursor_after,
                block_count=block_count,
            ),
            closed_call_ids=tuple(call.call_id for call in closed.calls),
            native_committed=closed.continuation.kind
            is not NativeContinuationKind.ABSENT,
        )
        self.observe_commit(receipt)
        return receipt


def _candidate_from(material) -> NativeCandidate:
    binding = material.origin.identity.binding
    return NativeCandidate(
        wire_shape=binding.wire_shape,
        endpoint_id=binding.endpoint_id,
        model_id=binding.model_id,
        payload_json=material.payload_json,
        call_ids=tuple(material.call_ids),
    )
