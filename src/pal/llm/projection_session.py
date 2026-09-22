"""Session-level endpoint projection owner (PLAN §3/P3).

One EndpointProjectionSession per logical conversation; single writer.  It
owns:

- the active EndpointBinding and its projection generation,
- the frontier cursor, advanced ONLY by trusted HistoryCommitReceipts,
- immutable projection chunks frozen at commit time: the wire items beyond
  the previous frontier in the round's last prepared request PLUS the
  round's newly accepted output (byte-true native assistant items or
  IR-encoded acceptance, review R3); a zero-freeze commit seals an EMPTY
  chunk that records the semantic span while its items stay in the open
  tail (review H1),
- the NativeContinuationStore for the active binding (destroyed on switch).

prepare() never re-normalizes; repair outcomes arrive as repaired
ClosedRounds from the shared runtime.  The fast path encodes only the tail
after the frontier; a generation change forces one full rebuild.  When the
required-native contract cannot be satisfied, prepare fails explicitly —
no silent semantic-only fallback (PLAN §0.2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from pal.llm.continuation_policy import (
    ContinuationDecisionKind,
    NativeCandidate,
    validate_candidate,
)
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    TextPartIR,
)
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
from pal.shared.json_values import freeze_json_mapping, thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR

__all__ = [
    "ProjectionChunk",
    "LeftReplacement",
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
    # v3 (PLAN §6.2): the semantic message ids this chunk covers.  The rebase
    # path (on_left_replaced) decides chunk survival from this span instead
    # of subtracting item counts; an empty span is a legacy chunk that can
    # no longer participate in a left replacement.
    semantic_span: tuple[str, ...] = ()
    # F5 (review af51d74): per-item semantic ownership aligned with ``items``
    # (same length; empty tuples are legacy items with unknown ownership).
    # Wire bytes frozen out of a pending tail keep their ORIGINAL owning
    # span, so a left replacement can retire them from a surviving chunk's
    # replay instead of letting compacted-away content ride along.
    item_spans: tuple[tuple[str, ...], ...] = ()
    # S1 (review 7d182fd): per-BLOCK semantic ownership aligned with both
    # ``items`` and ``item_spans``.  An Anthropic user-seam merge
    # concatenates two eras' blocks into ONE wire item; per-item spans
    # alone cannot retire just the retired-left blocks without dropping
    # the surviving right's blocks with them (whole-item keep/drop would
    # misdelete R).  Each entry aligns with that item's content blocks;
    # an empty entry marks an item without block ownership (whole-item
    # rules apply, e.g. legacy snapshots or non-list content).
    item_block_spans: tuple[tuple[tuple[str, ...], ...], ...] = ()


@dataclass(frozen=True)
class LeftReplacement:
    """Facts of one committed compact install (v3 PLAN §6.3).

    Carried by the history owner after replace-left: the new seed content,
    the semantic messages that survive in previously-frozen territory (the
    right side's already-committed part), and the post-install cursor base
    future append receipts must continue from.
    """

    seed_messages: tuple[LLMMessageIR, ...]
    kept_frozen_messages: tuple[LLMMessageIR, ...]
    cursor_after: HistoryCursor
    left_revision: int = 0
    # B1 (review 4b14ce4): model-view ids of EVERY message this seed
    # materializes.  A post-compact rebase seed is the standalone continuity
    # reference alone; a recovery/rebind bootstrap hands this entry the WHOLE
    # current L model view (summary plus promoted ordinary groups), so its
    # coverage is that whole view's id set — declaring one id while encoding
    # more made the next prepare re-append the rest as fresh tail.  The
    # coverage is established by the SAME encode that materializes the seed:
    # content first, declaration second, never a fabricated round chunk.
    seed_coverage_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _HeadSystemEntry:
    """Session-owned head system parts attributed to their round span (F4).

    Re-ownership after a left replacement is decided per entry from the
    surviving spans — retired left rounds cannot keep hoisted content
    alive in later requests.
    """

    span_ids: tuple[str, ...]
    parts: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _PendingWireItem:
    """One unfrozen wire item with the span that owns it (F5).

    Pending tail items are semantic territory: they were trimmed from a
    commit whose span ids name the round that produced them.  A left
    replacement retires pending entries whose owning span died instead of
    preserving the list wholesale and replaying compacted-away bytes after
    the summary.

    S1 (review 7d182fd): ``block_spans`` carries per-content-block
    ownership so a seam-merged item (retired-left blocks + surviving-right
    blocks in ONE user item) can retire exactly the left-owned blocks at
    the next left replacement.  Empty = no block ownership (whole-item
    rules; the item-level ``span_ids`` still applies).
    """

    item: dict
    span_ids: tuple[str, ...]
    block_spans: tuple[tuple[str, ...], ...] = ()


@dataclass
class _ActiveRound:
    attempt: AttemptKey
    requires_native: bool
    prepared_items: list[dict] = field(default_factory=list)
    # F5: per-item span ownership aligned with prepared_items.
    prepared_item_spans: list[tuple[str, ...]] = field(default_factory=list)
    # S1 (review 7d182fd): per-item BLOCK ownership aligned with
    # prepared_items (same length as prepared_item_spans).
    prepared_item_block_spans: list[tuple[tuple[str, ...], ...]] = field(
        default_factory=list
    )
    prepared_base_cursor: HistoryCursor | None = None
    # Top-level system parts hoisted from THIS round's tail-head messages
    # (review G2 persistence): container coordinates cannot freeze them, so
    # observe_commit transfers them into the session-owned head-system list.
    prepared_head_system: list[dict] = field(default_factory=list)


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
        # Validated endpoint capabilities for THIS lineage (review B1): the
        # binding's config_fingerprint summarizes the config but cannot
        # reconstitute it, so the encode profile (e.g.
        # unsupported_request_parameters) is supplied at bind time, kept
        # deeply frozen, and used by EVERY encode the session performs —
        # shell, tail, and accepted-message encodes share one profile.
        self._capabilities: Mapping[str, Any] = freeze_json_mapping({})
        # Amortized assembled prefix of all frozen chunk items; extended on
        # commit only, never rebuilt per prepare (PLAN §11: no per-round
        # deepcopy of old chunks).  Private mutable dicts: the public chunk
        # view holds deep-frozen snapshots (review R7).
        self._prefix_items: list[dict] = []
        # Per-item span ownership aligned with _prefix_items (F5).
        self._prefix_item_spans: list[tuple[str, ...]] = []
        # S1 (review 7d182fd): per-item BLOCK ownership aligned with
        # _prefix_items and _prefix_item_spans.
        self._prefix_item_block_spans: list[tuple[tuple[str, ...], ...]] = []
        # Unfrozen wire tail kept across rounds (review F2 + F5 ownership):
        # trailing items trimmed from a commit (e.g. Anthropic user-role tool
        # results) stay session-owned and are re-injected into every prepare
        # until a later commit freezes past them.  Each entry carries the
        # span of the round that produced it, so a left replacement retires
        # entries whose owning round died.  Semantic acceptance and wire
        # freezing are different coordinates.
        self._pending_wire_tail: list[_PendingWireItem] = []
        # Top-level system parts hoisted from past rounds' tail-head
        # system/developer messages (review G2 persistence).  Anthropic
        # renders request-head content outside the container, so committed
        # chunks cannot carry it; the session re-merges these parts into
        # every request after the fresh shell preamble, keeping the
        # incremental lineage equal to whole-history encoding.
        self._committed_head_system: list[_HeadSystemEntry] = []
        # Monotonic history-authority revision seen by this lineage (F4):
        # each left replacement must advance it, mirroring the root's cut.
        self._left_revision = 0
        # B1 (review 4b14ce4): model-view ids of every L message the current
        # replacement seed materialized (the standalone continuity form
        # included).  They are derived coverage: the assembled prefix already
        # carries them, so the next prepare must not re-append any of them as
        # fresh tail — not the summary, and not promoted ordinary history.
        # Replaced wholesale by the next left replacement; cleared by
        # bind/retire.
        self._l_coverage_ids: tuple[str, ...] = ()
        self._owner_fence = 0
        self.retired = False

    # -- binding lifecycle -------------------------------------------------

    def bind(
        self,
        binding: EndpointBinding,
        *,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        """Initial bind or rebind after switch; destroys the old lineage.

        ``capabilities`` (review B1) is the validated endpoint capability
        profile the caller resolved for THIS binding — the same data the
        legacy invoker reads from the endpoint (e.g.
        ``unsupported_request_parameters``).  It is deeply frozen here and
        reused by every encode in the lineage; a profile change is a new
        binding (fingerprint bump), never an in-place mutation.
        """

        if self.retired:
            raise ProjectionSessionError("session is retired")
        if capabilities is None:
            frozen_capabilities: Mapping[str, Any] = freeze_json_mapping({})
        else:
            if not isinstance(capabilities, Mapping):
                raise ProjectionSessionError(
                    "endpoint capabilities must be a mapping"
                )
            frozen_capabilities = freeze_json_mapping(dict(capabilities))
        generation = 0 if self.identity is None else self.identity.projection_generation + 1
        self.binding = binding
        self._capabilities = frozen_capabilities
        self.identity = ProjectionIdentity(
            session=self.session_id,
            binding=binding,
            projection_generation=generation,
        )
        # Endpoint switch destroys old active data (PLAN §8.1): native store,
        # derived chunks, frontier coverage, and any open round die together.
        # The owner fence survives: it counts worker ownership generations,
        # not endpoint bindings (review R7).
        self.native_by_attempt = {}
        self.chunks = ()
        self._frontier_item_count = 0
        self._committed_attempts = {}
        self.frontier = HistoryCursor.initial()
        self._prefix_items = []
        self._prefix_item_spans = []
        self._pending_wire_tail = []
        self._committed_head_system = []
        self._left_revision = 0
        self._l_coverage_ids = ()
        self._active = None

    def retire(self) -> None:
        self.retired = True
        self.native_by_attempt = {}
        self.chunks = ()
        self._prefix_items = []
        self._prefix_item_spans = []
        self._pending_wire_tail = []
        self._committed_head_system = []
        self._l_coverage_ids = ()
        self._active = None

    def _require_identity(self) -> ProjectionIdentity:
        if self.identity is None or self.binding is None:
            raise ProjectionSessionError("session is not bound to an endpoint")
        if self.retired:
            raise ProjectionSessionError("session is retired")
        return self.identity

    def _shape_context(self, *, has_conversation_prefix: bool = False) -> ShapeContext:
        """The ONE validated encode profile for this lineage (review B1).

        Every encode the session performs — shell envelope, tail, and
        accepted-message materialization — goes through this context, so a
        capability-restricted endpoint (e.g. one that rejects ``temperature``)
        omits unsupported optional fields exactly like the legacy full
        encode would.  Building contexts ad hoc with empty capabilities
        silently re-adds them.
        """

        return ShapeContext(
            wire_shape=self.binding.wire_shape,
            endpoint_id=self.binding.endpoint_id,
            model_id=self.binding.model_id,
            capabilities=self._capabilities,
            has_conversation_prefix=has_conversation_prefix,
        )

    # -- native continuation -----------------------------------------------

    def attach_native(self, attempt: AttemptKey, candidate: NativeCandidate) -> None:
        """Validate and store native material for an attempt.

        Degraded/unsupported REQUIRED material raises ContinuationUnavailable:
        the round cannot legally continue on this protocol, and silently
        re-encoding from IR is forbidden (PLAN §0.2/§6.3).

        Association is enforced, not assumed (review R7): the attempt must be
        the currently open round, and the candidate must come from the same
        binding (shape/endpoint/model).  A late native from a cancelled
        attempt or an alien provider must not enter the store.
        """

        self._require_identity()
        if attempt.identity != self.identity:
            raise ProjectionSessionError("attempt belongs to a different projection identity")
        if self._active is None or attempt != self._active.attempt:
            raise ProjectionSessionError(
                "native material may only attach to the currently open round's attempt"
            )
        if (
            candidate.wire_shape != self.binding.wire_shape
            or candidate.endpoint_id != self.binding.endpoint_id
            or candidate.model_id != self.binding.model_id
        ):
            raise ProjectionSessionError(
                "native candidate binding does not match the session binding"
            )
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
        record = self.native_by_attempt.get(attempt_id)
        if record is None:
            return None
        # Defensive copy: internal records stay private (review R7).
        return {
            "payload_json": record["payload_json"],
            "call_ids": tuple(record["call_ids"]),
            "endpoint_id": record["endpoint_id"],
            "model_id": record["model_id"],
        }

    # -- rounds and commits --------------------------------------------------

    def begin_round(self, attempt: AttemptKey, *, requires_native: bool) -> None:
        identity = self._require_identity()
        if self._active is not None:
            raise ProjectionSessionError("a round is already open for this session")
        if attempt.identity != identity:
            raise ProjectionSessionError("attempt identity does not match the session binding")
        if attempt.attempt_id in self._committed_attempts:
            # Committed attempts are TERMINAL (review C1): the idempotent
            # path for a finished attempt is replaying its receipt, never
            # reopening it as a draft.  Reopening would hand the cancel path
            # back to a finished attempt — close/reject would then drop its
            # already-committed native from the authoritative store while
            # receipt and chunk stay, and a re-attached replacement draft
            # could be exported as committed native by the snapshot filter
            # (which trusts ledger membership).  Checked before the fence
            # and _active move, so refusal leaves the session untouched.
            raise ProjectionSessionError(
                "attempt is already committed; replay its receipt instead of "
                "reopening the round"
            )
        if attempt.owner_fence.fence < self._owner_fence:
            # Owner fences move forward only: a stale worker cannot reopen
            # rounds after a reown (review R7).
            raise ProjectionSessionError(
                "attempt owner fence regresses below the session's current fence"
            )
        self._owner_fence = attempt.owner_fence.fence
        self._active = _ActiveRound(attempt=attempt, requires_native=requires_native)

    def close_round(self) -> AttemptKey:
        """Close the in-flight round without committing (cancel path).

        The round's UNACCEPTED native material dies with it (review B2):
        attach_native is draft-scoped until a commit accepts it, so a
        cancelled round must not leave its payload in the authoritative
        store or the next snapshot.
        """

        if self._active is None:
            raise ProjectionSessionError("no open round")
        attempt = self._active.attempt
        self.native_by_attempt.pop(attempt.attempt_id, None)
        self._active = None
        return attempt

    def observe_commit(
        self,
        receipt: HistoryCommitReceipt,
        *,
        accepted_messages: Sequence[LLMMessageIR] = (),
        span_message_ids: Sequence[str] = (),
    ) -> None:
        """Advance the frontier with a trusted joint commit (idempotent).

        ``span_message_ids`` (v3) names the semantic messages this commit
        covers beyond the previous frontier; they become the chunk's
        semantic_span so a later left replacement can decide survival.
        Repeated or out-of-order ids are refused.
        """

        span_ids = tuple(str(value) for value in span_message_ids)
        if len(set(span_ids)) != len(span_ids):
            raise ProjectionSessionError("commit span repeats a message id")

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
        # -- native eligibility is verified against stored material, never
        # -- taken on the caller's word (review R4).
        if self._active.requires_native and not receipt.native_committed:
            raise ProjectionSessionError(
                "round requires native continuation but the receipt commits none"
            )
        accepted_native: dict | None = None
        if receipt.native_committed:
            accepted_native = self.native_by_attempt.get(receipt.attempt.attempt_id)
            if accepted_native is None:
                raise ProjectionSessionError(
                    "native_committed receipt has no attached native material"
                )
            if tuple(accepted_native["call_ids"]) != tuple(receipt.closed_call_ids):
                raise ProjectionSessionError(
                    "native call inventory does not match the receipt's closed calls"
                )
        receipt.append.verify_against(self.frontier)
        if receipt.append.after.history_epoch != self.frontier.history_epoch:
            raise ProjectionSessionError("commit receipt epoch does not match frontier")
        # Materialize the accepted output beyond the prepared request input.
        materialized: list[dict] = []
        if accepted_native is not None:
            materialized.extend(
                dict(item)
                for item in _wire_items_from_native(
                    self.binding.wire_shape, accepted_native["payload_json"]
                )
            )
            # One representation per assistant contribution (review F3/G3):
            # when native carries the assistant turn — its tool calls AND its
            # plain text — accepted IR must not repeat ANY of it.  Only tool
            # results (a separate contribution) may join the commit.  A
            # text-only assistant IR message used to slip through and duplicate
            # the native answer (review G3).
            for message in accepted_messages:
                if message.role == MessageRole.ASSISTANT:
                    raise ProjectionSessionError(
                        "accepted IR messages repeat the assistant turn "
                        "already carried by the native material"
                    )
        if accepted_messages:
            materialized.extend(
                dict(item) for item in self._encode_messages(tuple(accepted_messages))
            )
        # S1 (review 7d182fd): accepted output beyond the frontier is
        # owned by THIS commit's span at block granularity — every content
        # block the encode materialized.  A later seam merge concatenates
        # blocks from two owners, so the per-block axis must exist here
        # first or mixed items could never be retired partially.
        materialized_block_spans = [
            _uniform_block_spans(item, span_ids) for item in materialized
        ]
        # Everything below is computed on LOCAL candidates and installed in
        # ONE block at the very end (review H1): a refusal must leave every
        # piece of session-visible state untouched, so neither a replayed
        # receipt nor a tail re-supplied from the unchanged frontier can ever
        # double content.  The open round's prepared_items is never mutated
        # either — a failed observe_commit can be retried deterministically.
        extended = [*self._active.prepared_items, *materialized]
        extended_spans = [
            *self._active.prepared_item_spans,
            *((span_ids,) * len(materialized)),
        ]
        if len(extended_spans) < len(extended):
            # Legacy draft state without ownership: pad with unknown spans.
            extended_spans.extend(
                [()] * (len(extended) - len(extended_spans))
            )
        extended_block_spans = [
            *self._active.prepared_item_block_spans,
            *materialized_block_spans,
        ]
        if len(extended_block_spans) < len(extended):
            extended_block_spans.extend(
                [()] * (len(extended) - len(extended_block_spans))
            )
        items = tuple(extended[self._frontier_item_count :])
        item_span_list = list(extended_spans[self._frontier_item_count :])
        item_block_span_list = list(
            extended_block_spans[self._frontier_item_count :]
        )
        if not items:
            raise ProjectionSessionError(
                "no prepared items beyond the frontier; commit has nothing to seal"
            )
        # Anthropic merges adjacent user-role wire messages, so a wire item
        # ending in role "user" is not a prefix-stable freeze point: the next
        # request's encoder would merge it with the following user message.
        # Trim trailing mergeable items back into the unfrozen tail; they are
        # re-encoded (bounded cost) until a later commit freezes past them.
        # F5: trimmed items keep their ORIGINAL owning span, so a left
        # replacement can retire them with the round that produced them.
        frozen_item_count = len(extended)
        unfrozen_suffix: list[_PendingWireItem] = []
        if self.binding.wire_shape.value == "anthropic_messages":
            while items and isinstance(items[-1], dict) and items[-1].get("role") == "user":
                unfrozen_suffix.insert(
                    0,
                    _PendingWireItem(
                        item=dict(items[-1]),
                        span_ids=tuple(item_span_list[-1]),
                        block_spans=tuple(item_block_span_list[-1]),
                    ),
                )
                items = items[:-1]
                item_span_list = item_span_list[:-1]
                item_block_span_list = item_block_span_list[:-1]
                frozen_item_count -= 1
        # A zero-freeze commit is ACCEPTED, not refused (review H1): Anthropic
        # can legitimately close a round whose items beyond the frontier are
        # ALL user-role (every unstarted call pruned, no preserved assistant
        # contribution).  Semantic coverage advances with the trusted receipt
        # while the full item span stays in the session-owned open tail — the
        # same rule F2 already applies to trailing user items — and the round
        # is sealed as an empty chunk so the chunk chain still ends at the
        # frontier for checkpoint/restore.  Refusing here instead would leave
        # the projection lineage permanently behind the durable L1 cursor.
        chunk = ProjectionChunk(
            round_attempt_id=receipt.attempt.attempt_id,
            cursor_before=self.frontier,
            cursor_after=receipt.append.after,
            # Deep-frozen public snapshot: mutating a committed chunk raises
            # instead of silently rewriting later requests (review R7).
            items=tuple(freeze_json_mapping(item) for item in items),
            prefix_digest=receipt.append.after.prefix_digest,
            semantic_span=span_ids,
            # F5: per-item ownership travels with the frozen snapshot so a
            # later rebase can retire retired-span bytes from replay.
            item_spans=tuple(tuple(span) for span in item_span_list),
            # S1 (review 7d182fd): per-block ownership travels alongside so
            # a later rebase can retire ONLY the retired-left blocks of a
            # seam-merged item while the surviving right's blocks stay.
            item_block_spans=tuple(
                tuple(blocks) for blocks in item_block_span_list
            ),
        )
        head_system_parts = [dict(part) for part in self._active.prepared_head_system]
        # -- single install boundary: no session-visible failure past here --
        self._pending_wire_tail = [
            _PendingWireItem(
                item=dict(entry.item),
                span_ids=tuple(entry.span_ids),
                block_spans=tuple(entry.block_spans),
            )
            for entry in unfrozen_suffix
        ]
        self.chunks = (*self.chunks, chunk)
        # The private amortized prefix keeps the mutable dicts; it is never
        # exposed and shares nothing with the frozen chunk snapshot above.
        self._prefix_items.extend(items)
        self._prefix_item_spans.extend(tuple(span) for span in item_span_list)
        self._prefix_item_block_spans.extend(
            tuple(blocks) for blocks in item_block_span_list
        )
        self._committed_attempts[receipt.attempt.attempt_id] = receipt
        # Request-head content hoisted to top-level system this round becomes
        # session-owned exactly like the wire tail (review G2 persistence),
        # attributed to the round's span so a left replacement can re-own
        # it (review F4).
        if head_system_parts:
            self._committed_head_system.append(
                _HeadSystemEntry(
                    span_ids=tuple(span_ids),
                    parts=tuple(
                        freeze_json_mapping(part) for part in head_system_parts
                    ),
                )
            )
        self.frontier = receipt.append.after
        self._frontier_item_count = frozen_item_count
        if not receipt.native_committed:
            # A commit that carried no native while the binding requires it
            # leaves the lineage unsealable for same-endpoint replay; the
            # chunk stays (derived), the native gap is explicit.
            self.native_by_attempt.pop(receipt.attempt.attempt_id, None)
        self._active = None

    def reject_commit(self, attempt_id: str, reason: str) -> None:
        """Drop an open round without sealing (late/failed response).

        Same draft-native rule as close_round (review B2): rejection never
        promotes unaccepted native material into the authoritative store.
        """

        _ = reason
        if self._active is None or self._active.attempt.attempt_id != attempt_id:
            raise ProjectionSessionError("no matching open round to reject")
        self.native_by_attempt.pop(attempt_id, None)
        self._active = None

    # -- v3 two-segment operations (PLAN §6.2) ------------------------------

    def prepare_normal(
        self,
        view: HistoryView,
        *,
        controls: dict | None = None,
        request_shell: LLMRequestIR | None = None,
    ) -> PreparedRequest:
        """Named normal-path prepare: base + frozen prefix + tail view."""

        return self.prepare(view, controls=controls, request_shell=request_shell)

    def rebind(
        self,
        binding: EndpointBinding,
        *,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        """Named endpoint switch: destroys the old lineage (PLAN §6.2).

        Compact install must NOT come through here — use on_left_replaced,
        which preserves right-side material in the same binding.
        """

        self.bind(binding, capabilities=capabilities)

    def prepare_handoff(
        self,
        left_messages: Sequence[LLMMessageIR],
        *,
        instruction: LLMMessageIR,
        attempt: AttemptKey,
        request_shell: LLMRequestIR | None = None,
        controls: dict | None = None,
    ) -> PreparedRequest:
        """Build the compact handoff request: base + full L + instruction.

        Independent-purpose view (PLAN §6.2): this encode never touches the
        normal round, the native store, the pending tail, or the accepted
        cursor — a failed or retried handoff leaves no trace in the normal
        lineage (P08).

        F3 (review): the base preamble — shell-owned system/developer
        heads, in-container for the OpenAI shapes, hoisted to the top level
        by the Anthropic codec — is part of ONE full encode of
        ``base + L + instruction``.  The payload is therefore byte-equal to
        a whole-history encode for every shape; no in-container preamble
        and no body-hoisted system is dropped on the floor.  The instruction
        rides as the trailing user message.
        """

        self._require_identity()
        if self._active is not None:
            raise ProjectionSessionError(
                "prepare_handoff cannot run inside an open normal round"
            )
        if attempt.identity != self.identity:
            raise ProjectionSessionError("handoff attempt belongs to another lineage")
        if instruction.role != MessageRole.USER:
            raise ProjectionSessionError("handoff instruction must be a user message")
        codec = codec_for_shape(self.binding.wire_shape)
        context = self._shape_context()
        shell = request_shell or LLMRequestIR(
            messages=(),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        # R4/M17 (review 95373ef): heads only — the LEADING system/developer
        # run of the shell.  Left-segment content (mid-conversation developer
        # contexts included) is carried by ``left_messages`` in its own
        # chronological position; hoisting every developer message would both
        # reorder the handoff bytes and duplicate L content.
        shell_messages = tuple(shell.messages)
        head_end = 0
        for message in shell_messages:
            if message.role.value in self._PREAMBLE_ROLES:
                head_end += 1
            else:
                break
        preamble = shell_messages[:head_end]
        body = (*preamble, *left_messages, instruction)
        encoded = codec.encode(replace(shell, messages=body), context)
        payload: dict[str, Any] = dict(encoded.payload)
        if controls:
            payload["controls"] = dict(controls)
        return PreparedRequest.build(
            attempt=attempt,
            base_cursor=self.frontier,
            payload=payload,
            message_spans=encoded.message_spans,
            extra_body=encoded.extra_body,
            applied_cache_breakpoint_message_ids=(
                encoded.applied_cache_breakpoint_message_ids
            ),
        )

    def on_left_replaced(self, change: LeftReplacement) -> None:
        """Rebase the lineage after a committed compact install (§6.3).

        Old-L chunks and their native material die; right-side chunks keep
        their ORIGINAL frozen wire items — the native-fidelity bytes carved
        at commit time — so the rebuilt prefix replays exactly what the
        provider saw (review F4), never an IR re-encode that silently drops
        native-only material.  Only the new seed pays a fresh encode (the
        generation-change cost, not a per-round path).  A chunk whose span
        straddles the replacement boundary is an illegal cut and fails
        loudly instead of corrupting the prefix.  Head-system parts
        re-own to their surviving rounds, and the history-authority
        revision must advance monotonically.
        """

        self._require_identity()
        if self._active is not None:
            raise ProjectionSessionError(
                "on_left_replaced cannot run inside an open normal round"
            )
        if int(change.left_revision) < self._left_revision:
            raise ProjectionSessionError(
                "left replacement does not advance the history authority "
                f"revision (have {self._left_revision}, "
                f"got {int(change.left_revision)})"
            )
        kept_ids = tuple(m.message_id for m in change.kept_frozen_messages)
        if len(set(kept_ids)) != len(kept_ids):
            raise ProjectionSessionError("kept frozen messages repeat an id")
        kept_set = set(kept_ids)
        surviving_chunks: list[ProjectionChunk] = []
        surviving_spans: list[str] = []
        for chunk in self.chunks:
            if not chunk.semantic_span:
                raise ProjectionSessionError(
                    "cannot rebase a lineage whose chunks lack semantic spans"
                )
            covered = set(chunk.semantic_span)
            if covered & kept_set:
                if covered <= kept_set:
                    surviving_chunks.append(chunk)
                    surviving_spans.extend(chunk.semantic_span)
                    continue
                raise ProjectionSessionError(
                    "replacement boundary splits a frozen round; illegal cut"
                )
        if sorted(surviving_spans) != sorted(kept_ids):
            raise ProjectionSessionError(
                "kept frozen messages do not match the surviving chunks"
            )
        codec = codec_for_shape(self.binding.wire_shape)
        context = self._shape_context()
        container = _ITEM_CONTAINER_KEY.get(self.binding.wire_shape.value, "input")
        rebuilt_items: list[dict] = []
        rebuilt_spans: list[tuple[str, ...]] = []
        rebuilt_block_spans: list[tuple[tuple[str, ...], ...]] = []
        # S1 (review 7d182fd): the seed's wire bytes are OWNED BY the left
        # reference its coverage names — not unconditionally-owned flotsam.
        # An empty span used to make seed bytes survive every later left
        # replacement (the pending-tail path) and, after a seam merge,
        # ride inside a surviving right chunk's user item.  With the
        # coverage as the span the NEXT replacement retires them; an empty
        # coverage only occurs for legacy callers and keeps the old
        # conservative (unconditional) behavior.
        seed_span = tuple(
            str(value) for value in (change.seed_coverage_ids or ())
        )
        if change.seed_messages:
            encoded = codec.encode(
                LLMRequestIR(
                    messages=tuple(change.seed_messages),
                    tools=(),
                    policy=GenerationPolicyIR(max_output_tokens=4096),
                ),
                context,
            )
            seed_items = [
                dict(thaw_json(item))
                for item in (dict(encoded.payload).get(container) or [])
            ]
            rebuilt_items.extend(seed_items)
            rebuilt_spans.extend([seed_span] * len(seed_items))
            rebuilt_block_spans.extend(
                _uniform_block_spans(item, seed_span) for item in seed_items
            )
        # F4: surviving right chunks replay their ORIGINAL wire items —
        # byte-true native material included — instead of an IR re-encode.
        # thaw_json: the private prefix owns MUTABLE copies; the chunk's
        # public snapshot is deep-frozen and must not leak proxies into
        # later encodes.  F5: when the chunk carries per-item ownership,
        # items whose owning span retired die here — wire bytes frozen out
        # of a retired round's pending tail must not ride a surviving
        # chunk.  Legacy chunks without ownership replay whole.
        # S1 (review 7d182fd): with per-BLOCK ownership the filter RETIRES
        # ONLY the retired-left blocks of a seam-merged item and rebuilds
        # the item from the surviving blocks — whole-item keep/drop would
        # either resurrect the retired left (span names only right ids) or
        # misdelete the surviving right (union span escapes kept_set).
        for chunk in surviving_chunks:
            spans = chunk.item_spans
            blocks_per_item = chunk.item_block_spans
            has_item_spans = bool(spans) and len(spans) == len(chunk.items)
            has_block_spans = (
                bool(blocks_per_item) and len(blocks_per_item) == len(chunk.items)
            )
            for index, item in enumerate(chunk.items):
                item_span = tuple(spans[index]) if has_item_spans else ()
                item_blocks = (
                    tuple(blocks_per_item[index]) if has_block_spans else ()
                )
                surviving = _retire_wire_item(
                    thaw_json(item), item_span, item_blocks, kept_set
                )
                if surviving is None:
                    continue
                kept_item, kept_span, kept_blocks = surviving
                rebuilt_items.append(dict(kept_item))
                rebuilt_spans.append(kept_span)
                rebuilt_block_spans.append(kept_blocks)
        # F5: the session-owned pending tail is right-side territory by
        # definition (items trimmed from commits that are not yet frozen),
        # but each entry belongs to the ROUND that produced it: entries
        # whose owning span died with the compacted-away L retire here
        # instead of reappearing after the summary.  S1: block ownership
        # retires only the retired-left blocks of a merged entry.
        surviving_pending = [
            entry
            for entry in (
                _retire_wire_item(
                    thaw_json(entry.item),
                    tuple(entry.span_ids),
                    tuple(entry.block_spans),
                    kept_set,
                )
                for entry in self._pending_wire_tail
            )
            if entry is not None
        ]
        rebuilt_items.extend(kept_item for kept_item, _, _ in surviving_pending)
        rebuilt_spans.extend(kept_span for _, kept_span, _ in surviving_pending)
        rebuilt_block_spans.extend(
            kept_blocks for _, _, kept_blocks in surviving_pending
        )
        # Anthropic merges adjacent user-role wire messages, so a rebuilt
        # prefix ending in role "user" is not a stable freeze point (the
        # same rule commit-time trimming applies).  Hand the trailing user
        # items to the open tail so the next prepare merges them with the
        # incoming tail exactly like a whole-history encode would.
        unfrozen_tail: list[_PendingWireItem] = []
        if self.binding.wire_shape.value == "anthropic_messages":
            while rebuilt_items and isinstance(rebuilt_items[-1], dict) \
                    and rebuilt_items[-1].get("role") == "user":
                unfrozen_tail.insert(
                    0,
                    _PendingWireItem(
                        item=rebuilt_items.pop(),
                        span_ids=rebuilt_spans.pop(),
                        block_spans=rebuilt_block_spans.pop(),
                    ),
                )
        # -- single install section.
        surviving_attempt_ids = {chunk.round_attempt_id for chunk in surviving_chunks}
        dead_attempts = {
            chunk.round_attempt_id
            for chunk in self.chunks
            if chunk.round_attempt_id not in surviving_attempt_ids
        }
        for attempt_id in dead_attempts:
            self.native_by_attempt.pop(attempt_id, None)
            self._committed_attempts.pop(attempt_id, None)
        self.chunks = tuple(surviving_chunks)
        self._prefix_items = rebuilt_items
        self._prefix_item_spans = rebuilt_spans
        self._prefix_item_block_spans = rebuilt_block_spans
        self._pending_wire_tail = unfrozen_tail
        # B1 (review 4b14ce4): the replacement's seed coverage becomes the
        # lineage's L-owned coverage from this moment on — every model-view id
        # the seed encode materialized, so the next prepare never re-appends
        # L content the rebuilt prefix already carries.
        self._l_coverage_ids = tuple(
            str(value) for value in (change.seed_coverage_ids or ())
        )
        # F4: head-system parts re-own to their surviving rounds; entries
        # attributed to retired left rounds die with them.
        self._committed_head_system = [
            entry for entry in self._committed_head_system
            if set(entry.span_ids) <= kept_set
        ]
        self.frontier = change.cursor_after
        self._frontier_item_count = len(rebuilt_items)
        self._left_revision = int(change.left_revision)

    def frozen_message_ids(self) -> tuple[str, ...]:
        """Semantic message ids already frozen into committed chunks (read).

        The history owner's rebase caller uses this to pass exactly the
        frozen right-side messages as ``kept_frozen_messages`` — unfrozen
        right messages stay in the open tail and must not claim survival.
        """

        return tuple(
            dict.fromkeys(
                message_id
                for chunk in self.chunks
                for message_id in chunk.semantic_span
            )
        )

    def covered_message_ids(self) -> tuple[str, ...]:
        """Semantic ids the assembled prefix already covers (R3/B1 read).

        Committed chunks' spans PLUS the L-owned coverage of the current left
        materialization (every model-view message the replacement seed
        encoded — the standalone continuity form included).  The next prepare
        must not re-append any of them as a fresh tail; a seed that lived
        only as empty-span prefix bytes used to be re-injected exactly that
        way, doubling the summary on the wire, and a recovery bootstrap with
        promoted ordinary history re-appended every non-summary L message
        (review 4b14ce4 B1).
        """

        return tuple(
            dict.fromkeys((*self.frozen_message_ids(), *self._l_coverage_ids))
        )

    @property
    def history_left_revision(self) -> int:
        """Left-replacement generation this lineage has consumed (read).

        The turn projection compares it against the root's left_generation:
        a mismatch means a compact install happened whose rebase this
        session never consumed — the frozen prefix is stale and the next
        round must fall back cold instead of replaying retired history.
        """

        return self._left_revision

    def has_materialized_content(self) -> bool:
        """Whether this lineage holds any frozen or pending wire bytes (read).

        C3 (review c9cb2d2): the turn projection distinguishes a fresh or
        rebound lineage — which owns no wire content and therefore cannot be
        replaying retired history — from a materialized one whose frozen
        prefix may be behind the current left generation.  Only the former
        may cold-build from the current canonical L/R once; a materialized
        lineage keeps the strict stale refusal until an explicit rebase.
        Session-visible state only.
        """

        return bool(
            self.chunks
            or self._prefix_items
            or self._pending_wire_tail
            or self._committed_head_system
            or self._l_coverage_ids
        )

    # -- preparation ---------------------------------------------------------

    _PREAMBLE_ROLES = frozenset({"system", "developer"})

    def prepare(
        self,
        view: HistoryView,
        *,
        controls: dict | None = None,
        request_shell: LLMRequestIR | None = None,
    ) -> PreparedRequest:
        """Build the next request: preamble + frozen chunks + pending + tail.

        Fast path requires the view cursor to equal the frontier (the L1
        writer and the session agree through the receipt chain).  A cursor
        mismatch is an explicit failure — same length is not an append proof.

        Shell/tail separation (review F1): ``request_shell`` owns the stable
        request envelope — its ``messages`` are the preamble (system /
        developer heads), its tools/policy drive the codec fields (model,
        max_tokens, tool definitions, the Anthropic top-level ``system``).
        The shell is encoded VERBATIM on every prepare (O(preamble)), so the
        preamble survives every incremental round and honors per-request
        budgets; only preamble-role messages from the shell enter the item
        array (conversation heads in the shell stay with the conversation,
        where the frontier already covers them).  The tail is encoded
        separately with an explicit boundary-context flag so position-
        sensitive projections (Completion developer promotion) match
        whole-history encoding.
        """

        self._require_identity()
        if self._active is None:
            raise ProjectionSessionError("prepare requires an open round (begin_round first)")
        if view.cursor != self.frontier:
            raise ProjectionSessionError(
                "history view cursor does not match the projection frontier; "
                "append proof missing"
            )
        codec = codec_for_shape(self.binding.wire_shape)
        context = self._shape_context()
        container = _ITEM_CONTAINER_KEY.get(self.binding.wire_shape.value, "input")
        shell = request_shell or LLMRequestIR(
            messages=(),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        # 1) Shell envelope, encoded verbatim each prepare (small, stable).
        #    S2 (review 95373ef): the envelope encode must NOT scan the whole
        #    history — callers hand the complete effective request as the
        #    shell, but only the preamble belongs to this encode; the
        #    conversation part is re-encoded with the tail view below.
        #    Encoding the whole request here was O(history) codec work twice
        #    per round.  Even a message-less shell (pure policy/tools change)
        #    produces envelope fields; the codec's empty-request fallback
        #    items live in the container and are simply discarded.
        #
        #    R4/M17 (review 95373ef): the preamble is the LEADING run of
        #    system/developer messages only — "heads", not every developer
        #    message anywhere in the shell.  Mid-conversation developer
        #    contexts (per-turn pal_context revisions) stay with the
        #    conversation, exactly like a whole-history encode keeps them
        #    (codecs place them chronologically, degrading mid-stream
        #    guidance to a chronological block).  Hoisting ALL developer
        #    messages used to move those revisions ahead of the frozen
        #    prefix, so a new revision reordered bytes mid-payload and broke
        #    both the cold-equality invariant and the cached prefix.
        shell_fields: dict[str, Any] = {}
        preamble_items: list[dict] = []
        shell_messages = tuple(shell.messages)
        head_end = 0
        for message in shell_messages:
            if message.role.value in self._PREAMBLE_ROLES:
                head_end += 1
            else:
                break
        preamble_only = shell_messages[:head_end]
        encoded_shell = codec.encode(
            replace(shell, messages=preamble_only), context
        )
        shell_payload = dict(encoded_shell.payload)
        shell_fields = {
            key: value for key, value in shell_payload.items() if key != container
        }
        if preamble_only:
            # The envelope encode IS the preamble encode; only wire items
            # actually mapped to a preamble message (via the codec's own
            # spans) enter the request.  The codec's empty-array fallback
            # ("Continue.") has no span and is excluded here.
            spanned_indexes: set[int] = set()
            for span in encoded_shell.message_spans:
                for path in span.wire_item_paths:
                    if len(path) >= 2 and path[0] == container and isinstance(path[1], int):
                        spanned_indexes.add(path[1])
            container_items = list(shell_payload.get(container) or [])
            preamble_items = [
                dict(container_items[index])
                for index in sorted(spanned_indexes)
                if index < len(container_items)
            ]
        # 2) Tail encode with explicit boundary context: position-sensitive
        # projections must see that a CONVERSATION prefix precedes this batch
        # even though the frozen items are not in this encode list.  The
        # preamble is deliberately excluded from this flag: shell-owned head
        # content does not make a tail message mid-conversation.  Including it
        # made a round-one tail developer message degrade while whole-history
        # encoding still promoted it (review G2).
        tail_payload: dict[str, Any] = {}
        tail_spans: tuple = ()
        if view.messages:
            tail_context = replace(
                context,
                has_conversation_prefix=bool(
                    self._prefix_items or self._pending_wire_tail
                ),
            )
            encoded_tail = codec.encode(replace(shell, messages=view.messages), tail_context)
            tail_payload = dict(encoded_tail.payload)
            tail_items = [
                dict(item)
                for item in (tail_payload.get(container) or [])
            ]
            tail_spans = tuple(encoded_tail.message_spans)
        else:
            tail_items = []
        # Conversation-span items only (review G1): the preamble NEVER enters
        # the frozen conversation stream, so commit accounting over
        # prepared_items uses conversation coordinates exclusively — a
        # request-with-preamble array can no longer be sliced with a
        # conversation-only cursor, and a committed chunk can no longer
        # freeze the preamble into the prefix.  Preamble items are re-injected
        # fresh at assembly time below, honoring per-request shell budgets.
        # F5: each conversation item carries its owning span — frozen prefix
        # items keep their historical span, pending entries keep the span of
        # the round that produced them, and this round's tail items are
        # attributed to the tail message ids.
        prefix_spans = list(self._prefix_item_spans)
        if len(prefix_spans) < len(self._prefix_items):
            prefix_spans.extend([()] * (len(self._prefix_items) - len(prefix_spans)))
        prefix_block_spans = list(self._prefix_item_block_spans)
        if len(prefix_block_spans) < len(self._prefix_items):
            prefix_block_spans.extend(
                [()] * (len(self._prefix_items) - len(prefix_block_spans))
            )
        tail_message_ids = tuple(
            str(message.message_id or "") for message in view.messages
        )
        # S1 (review 7d182fd): each conversation item now carries (item,
        # item_span, block_spans) — the per-block axis survives the seam
        # merge below so a later left replacement can retire exactly the
        # retired-left blocks of a merged user item.
        conversation: list[tuple[dict, tuple[str, ...], tuple[tuple[str, ...], ...]]] = [
            (dict(item), tuple(span), tuple(blocks))
            for item, span, blocks in zip(
                self._prefix_items, prefix_spans, prefix_block_spans
            )
        ]
        conversation.extend(
            (dict(entry.item), tuple(entry.span_ids), tuple(entry.block_spans))
            for entry in self._pending_wire_tail
        )
        conversation.extend(
            (
                dict(item),
                tail_message_ids,
                _uniform_block_spans(item, tail_message_ids),
            )
            for item in tail_items
        )
        # Anthropic merges adjacent user-role wire messages (source-verified
        # _append_message behavior), so the pending/tail boundary must merge
        # the same way a whole-history encode would — otherwise assembled
        # requests differ from full encodings by one split user message.
        # Boundary index is in conversation coordinates (review G1); the
        # merged item owns the UNION of both sides' spans (F5).
        anthropic_boundary_merged = False
        if (
            self.binding.wire_shape.value == "anthropic_messages"
            and self._pending_wire_tail
            and tail_items
        ):
            boundary = len(self._prefix_items) + len(self._pending_wire_tail) - 1
            conversation_length_before = len(conversation)
            conversation = _merge_anthropic_user_boundary_pairs(
                conversation, boundary
            )
            # R4 (review 95373ef): the remap transform may only claim a merge
            # that actually happened.  Pending+tail PRESENCE is not a merge —
            # a user USER boundary keeps both items, and a user + assistant
            # boundary never merges at all.
            anthropic_boundary_merged = len(conversation) != conversation_length_before
        self._active.prepared_items = [item for item, _, _ in conversation]
        self._active.prepared_item_spans = [
            span for _, span, _ in conversation
        ]
        self._active.prepared_item_block_spans = [
            blocks for _, _, blocks in conversation
        ]
        conversation_items = self._active.prepared_items
        self._active.prepared_base_cursor = view.cursor
        assembled: list[dict] = [
            *(dict(item) for item in preamble_items),
            *conversation_items,
        ]
        # The Completion codec merges adjacent same-role system text at encode
        # time (_append_chat_message); the fresh-preamble|frozen-conversation
        # seam must merge the same way, or the assembled request differs from
        # a whole-history encoding by one split system message (review G1/G2
        # seam).  The preamble side is always a fresh copy, so merging here
        # never mutates the frozen prefix.
        seam_merged = False
        if (
            self.binding.wire_shape.value == "openai_completion"
            and preamble_items
            and len(assembled) > len(preamble_items)
        ):
            seam = len(preamble_items) - 1
            left, right = assembled[seam], assembled[seam + 1]
            if (
                isinstance(left, dict)
                and isinstance(right, dict)
                and left.get("role") == "system"
                and right.get("role") == "system"
                and isinstance(left.get("content"), str)
                and isinstance(right.get("content"), str)
            ):
                merged = dict(left)
                merged["content"] = _merge_system_instruction_text(
                    str(left["content"]), str(right["content"])
                )
                assembled = [
                    *assembled[:seam],
                    merged,
                    *assembled[seam + 2 :],
                ]
                # R4 (review 95373ef): the merge shrinks the assembled item
                # array by one; every conversation/tail coordinate below
                # must consume that REAL transform instead of assuming the
                # preamble size is unchanged.
                seam_merged = True
        payload: dict[str, Any] = {**shell_fields, container: assembled}
        # W2/warm (主项1): spans travel WITH the assembled projection so the
        # prompt-cache coordinator can plan anchors/frontiers on the PROJECTED
        # payload exactly like a cold encode — without them the projected path
        # silently bypasses explicit cache planning and can never confirm a
        # warm anchor.  Preamble spans index the assembled head unchanged;
        # tail spans remap into conversation coordinates, including the
        # anthropic boundary merge (merged item = pending[-1] + tail[0]).
        merged_boundary = (
            len(preamble_items) + len(self._prefix_items)
            + len(self._pending_wire_tail) - 1
            if anthropic_boundary_merged
            else None
        )
        _left_pending = self._pending_wire_tail[-1].item if self._pending_wire_tail else {}
        merged_left_blocks = (
            len(_left_pending.get("content") or [])
            if merged_boundary is not None
            and isinstance(_left_pending.get("content"), list)
            else 0
        )
        tail_container_start = (
            len(preamble_items)
            - (1 if seam_merged else 0)
            + len(self._prefix_items)
            + len(self._pending_wire_tail)
        )
        assembled_spans: list = [
            *(
                span for span in encoded_shell.message_spans
                if span.message_id in {
                    message.message_id for message in preamble_only
                }
            ),
            *(
                _remap_tail_span(
                    span,
                    container=container,
                    container_start=tail_container_start,
                    merged_boundary=merged_boundary,
                    merged_left_blocks=merged_left_blocks,
                )
                for span in tail_spans
            ),
        ]
        # Position-sensitive codecs may hoist tail-head system/developer
        # content into the TAIL encode's top-level ``system`` when this batch
        # sits at the request head (no conversation prefix).  Those parts are
        # request content, not envelope noise: they are re-merged AFTER the
        # shell's fresh preamble and BEFORE previously committed head parts,
        # instead of being silently dropped (review G2).  Committed head
        # parts are session-owned across rounds; this round's tail parts
        # transfer to that list at commit time.
        tail_system_parts: list[dict] = []
        raw_tail_system = tail_payload.get("system")
        if isinstance(raw_tail_system, (list, tuple)):
            tail_system_parts = [
                dict(part)
                for part in raw_tail_system
                if isinstance(part, Mapping)
            ]
        self._active.prepared_head_system = tail_system_parts
        merged_system: list[dict] = [
            *(dict(thaw_json(part)) for entry in self._committed_head_system
              for part in entry.parts),
            *tail_system_parts,
        ]
        if merged_system:
            shell_system = shell_fields.get("system")
            if isinstance(shell_system, (list, tuple)):
                payload["system"] = [
                    *(dict(part) for part in shell_system),
                    *merged_system,
                ]
            else:
                payload["system"] = merged_system
        if controls:
            payload["controls"] = dict(controls)
        # R4/M17 (review 95373ef): finalize the assembled spans against the
        # payload ACTUALLY being sent.  The tail spans' fingerprints and
        # estimates described the tail-only encode while their paths were
        # remapped into assembled coordinates — a map that did not point at
        # the wire it claimed.  Cold encodes are finalized inside the codecs;
        # the projected path must describe the same bytes to plan anchors and
        # frontiers "exactly like a cold encode" (and to keep the stall
        # diagnostics fed with a truthful growing prefix estimate).
        from pal.llm.shapes.base import EncodedRequest as _AssembledRequest
        from pal.llm.shapes.base import finalize_cache_spans

        finalized = finalize_cache_spans(
            _AssembledRequest(payload=payload, message_spans=tuple(assembled_spans))
        )
        return PreparedRequest.build(
            attempt=self._active.attempt,
            base_cursor=view.cursor,
            payload=payload,
            message_spans=tuple(finalized.message_spans),
            extra_body=dict(encoded_shell.extra_body or {}),
        )

    # -- repair ---------------------------------------------------------------

    def accept_repaired_round(
        self,
        closed: ClosedRound,
        *,
        cursor_after: HistoryCursor,
        block_count: int,
    ) -> HistoryCommitReceipt:
        """Seal a repaired round produced by the shared runtime.

        The session never repairs by itself; it only accepts runtime-validated
        repaired rounds (dangling calls pruned, native association intact).
        The repaired calls/results are materialized into the chunk as IR
        blocks encoded by the shape codec (review R3): a legally preserved
        call/result pair must reach later requests, not only the receipt.
        """

        from pal.llm.projection_contracts import AppendReceipt, ToolOutcome

        if self._active is None or closed.attempt != self._active.attempt:
            raise ProjectionSessionError("repaired round does not match the open round")
        # One representation per assistant contribution (review F3): when the
        # continuation carries native material (REQUIRED or OPTIONAL), the
        # native payload already contains the assistant turn including its
        # tool calls; IR rebuilds would duplicate them.  Native material is
        # attached from the ClosedRound itself (no reliance on an earlier
        # side-channel attach).  Tool results are separate contributions and
        # always materialize as IR.
        has_native = closed.continuation.kind is not NativeContinuationKind.ABSENT
        if has_native:
            material = closed.continuation.material
            if material is None:
                raise ContinuationUnavailable("repaired round lost its native material")
            self.attach_native(closed.attempt, _candidate_from(material))
        accepted: list[LLMMessageIR] = []
        if not has_native:
            # The repaired round's preserved assistant contribution (review
            # H2): texts first, then calls, in one assistant message.  A
            # text-only repair (all calls pruned) materializes the text alone;
            # an inventory-only repair keeps the previous behavior.
            assistant_parts = [
                TextPartIR(text) for text in closed.assistant_texts
            ]
            assistant_parts.extend(
                ToolCallIR(
                    call.call_id,
                    call.name,
                    json.loads(call.arguments_json),
                )
                for call in closed.calls
            )
            if assistant_parts:
                accepted.append(
                    LLMMessageIR(
                        role=MessageRole.ASSISTANT,
                        parts=tuple(assistant_parts),
                    )
                )
        if closed.results:
            names = {call.call_id: call.name for call in closed.calls}
            accepted.append(
                LLMMessageIR(
                    role=MessageRole.TOOL,
                    parts=tuple(
                        ToolResultIR(
                            result.call_id,
                            names.get(result.call_id) or result.call_id,
                            result.body,
                            ok=result.outcome is ToolOutcome.SUCCESS,
                        )
                        for result in closed.results
                    ),
                )
            )
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
        self.observe_commit(receipt, accepted_messages=tuple(accepted))
        return receipt

    def _encode_messages(self, messages: tuple[LLMMessageIR, ...]) -> list[dict]:
        codec = codec_for_shape(self.binding.wire_shape)
        context = self._shape_context()
        container = _ITEM_CONTAINER_KEY.get(self.binding.wire_shape.value, "input")
        encoded = codec.encode(
            LLMRequestIR(
                messages=messages,
                tools=(),
                policy=GenerationPolicyIR(max_output_tokens=4096),
            ),
            context,
        )
        return list(dict(encoded.payload).get(container) or [])


def _merge_system_instruction_text(left: str, right: str) -> str:
    """Mirror of the Completion codec's system-text merge (keep in sync)."""

    if left.strip() and right.strip():
        return f"{left.rstrip()}\n\n{right.lstrip()}"
    return left or right


def _merge_anthropic_user_boundary(items: list[dict], boundary: int) -> tuple[bool, list[dict]]:
    """Merge the pending/tail boundary the way Anthropic's encoder would.

    Mirrors the codec's _append_message merge for adjacent user messages
    with list content: content blocks concatenate into one message.  This is
    the explicit boundary rule that keeps assembled incremental requests
    byte-equal to whole-history encodings (review F2).
    """

    if boundary < 0 or boundary + 1 >= len(items):
        return False, items
    left = items[boundary]
    right = items[boundary + 1]
    if (
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.get("role") == "user"
        and right.get("role") == "user"
        and isinstance(left.get("content"), list)
        and isinstance(right.get("content"), list)
    ):
        merged_item = dict(left)
        merged_item["content"] = [*left["content"], *right["content"]]
        return True, [*items[:boundary], merged_item, *items[boundary + 2 :]]
    return False, items


def _remap_path_into_assembled(
    path: tuple,
    *,
    container: str,
    container_start: int,
    merged_boundary: int | None,
    merged_left_blocks: int,
) -> tuple:
    """Remap one tail-encode JSON path into assembled-payload coordinates."""

    if len(path) < 2 or path[0] != container or not isinstance(path[1], int):
        return path
    rest = list(path[2:])
    index = container_start + int(path[1])
    if merged_boundary is not None:
        if int(path[1]) == 0:
            # The first tail message merged into the pending user item:
            # its content blocks sit AFTER the pending item's blocks.
            index = merged_boundary
            if len(rest) >= 2 and rest[0] == "content" and isinstance(rest[1], int):
                rest[1] = int(rest[1]) + merged_left_blocks
        else:
            index -= 1
    return (path[0], index, *rest)


def _remap_tail_span(
    span,
    *,
    container: str,
    container_start: int,
    merged_boundary: int | None,
    merged_left_blocks: int,
):
    """Remap a tail-encode span into assembled-payload coordinates (warm)."""

    remap_args = {
        "container": container,
        "container_start": container_start,
        "merged_boundary": merged_boundary,
        "merged_left_blocks": merged_left_blocks,
    }
    return replace(
        span,
        cache_targets=tuple(
            _remap_path_into_assembled(path, **remap_args)
            for path in (span.cache_targets or ())
        ),
        wire_item_paths=tuple(
            _remap_path_into_assembled(path, **remap_args)
            for path in (span.wire_item_paths or ())
        ),
        # R4 (review 95373ef): the continuity target is a path like every
        # other — it consumes the same real transform, or the summary anchor
        # keeps a stale tail-local coordinate (or none at all).
        continuity_target=(
            _remap_path_into_assembled(span.continuity_target, **remap_args)
            if span.continuity_target
            else ()
        ),
    )


def _uniform_block_spans(
    item: Mapping[str, Any], span: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    """S1 (review 7d182fd): block ownership for one wholly-owned item.

    An item whose whole content belongs to ONE owner gets one span per
    content block (list content only); non-list content has no block axis
    and keeps whole-item rules.
    """

    content = item.get("content") if isinstance(item, Mapping) else None
    if not isinstance(content, list):
        return ()
    return tuple(span for _ in content)


def _effective_block_spans(
    item: Mapping[str, Any],
    span: tuple[str, ...],
    block_spans: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    """S1: usable block ownership for ``item``, falling back to the whole
    item's span when the per-block axis is absent or misaligned."""

    content = item.get("content") if isinstance(item, Mapping) else None
    if (
        block_spans
        and isinstance(content, list)
        and len(block_spans) == len(content)
    ):
        return block_spans
    return _uniform_block_spans(item, span)


def _retire_wire_item(
    item: Any,
    span: tuple[str, ...],
    block_spans: tuple[tuple[str, ...], ...],
    kept_set: set[str],
) -> tuple[Mapping[str, Any], tuple[str, ...], tuple[tuple[str, ...], ...]] | None:
    """S1 (review 7d182fd): retire ONLY the blocks a retired left owned.

    Returns the surviving ``(item, item_span, block_spans)`` or ``None``
    when nothing survives.  Items with usable block ownership keep each
    block whose owning span is empty (unconditional/legacy) or inside
    ``kept_set``; the item is REBUILT from the surviving blocks when only
    some die, so a seam-merged user item loses exactly the retired-left
    blocks while the surviving right's blocks stay (S-I2/S-I3).  Items
    without the block axis keep the F5 whole-item rule.
    """

    content = item.get("content") if isinstance(item, Mapping) else None
    if (
        block_spans
        and isinstance(content, list)
        and len(block_spans) == len(content)
    ):
        kept_pairs = [
            (block, tuple(block_span))
            for block, block_span in zip(content, block_spans)
            if not block_span or set(block_span) <= kept_set
        ]
        if not kept_pairs:
            return None
        if len(kept_pairs) == len(content):
            return item, tuple(span), tuple(block_spans)
        rebuilt = dict(item)
        rebuilt["content"] = [block for block, _ in kept_pairs]
        merged_span = tuple(
            dict.fromkeys(
                block_span for _, block_span in kept_pairs if block_span
            )
        )
        return (
            rebuilt,
            merged_span,
            tuple(kept_block_span for _, kept_block_span in kept_pairs),
        )
    if span and not set(span) <= kept_set:
        return None
    return item, tuple(span), tuple(block_spans)


def _merge_anthropic_user_boundary_pairs(
    conversation: list[tuple[dict, tuple[str, ...], tuple[tuple[str, ...], ...]]],
    boundary: int,
) -> list[tuple[dict, tuple[str, ...], tuple[tuple[str, ...], ...]]]:
    """Span-aware mirror of _merge_anthropic_user_boundary (F5).

    The merged item owns the UNION of both sides' item spans.  S1 (review
    7d182fd): the per-BLOCK ownership concatenates in the same order the
    content blocks do, so each era's blocks keep their own owner and a
    later left replacement retires exactly the retired-left blocks —
    never the whole merged item (that would misdelete R) and never none
    of it (that would resurrect the retired left).
    """

    if boundary < 0 or boundary + 1 >= len(conversation):
        return conversation
    left_triple = conversation[boundary]
    right_triple = conversation[boundary + 1]
    left, right = left_triple[0], right_triple[0]
    if (
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.get("role") == "user"
        and right.get("role") == "user"
        and isinstance(left.get("content"), list)
        and isinstance(right.get("content"), list)
    ):
        merged_item = dict(left)
        merged_item["content"] = [*left["content"], *right["content"]]
        merged_span = tuple(dict.fromkeys((*left_triple[1], *right_triple[1])))
        merged_blocks = (
            *_effective_block_spans(left, left_triple[1], left_triple[2]),
            *_effective_block_spans(right, right_triple[1], right_triple[2]),
        )
        return [
            *conversation[:boundary],
            (merged_item, merged_span, merged_blocks),
            *conversation[boundary + 2 :],
        ]
    return conversation


def _candidate_from(material) -> NativeCandidate:
    binding = material.origin.identity.binding
    return NativeCandidate(
        wire_shape=binding.wire_shape,
        endpoint_id=binding.endpoint_id,
        model_id=binding.model_id,
        payload_json=material.payload_json,
        call_ids=tuple(material.call_ids),
    )


def _wire_items_from_native(shape_value: str, payload_json: str) -> list[dict]:
    """Project a provider-native response payload into request wire items.

    This is the byte-true path (review R3/R4): the assistant turn that the
    provider returned is replayed exactly as the provider shaped it, so
    signatures/encrypted reasoning survive instead of being re-encoded.
    """

    payload = json.loads(payload_json)
    if shape_value == "openai_completion":
        message = payload.get("message")
        return [dict(message)] if isinstance(message, dict) else []
    if shape_value == "openai_response":
        output = payload.get("output")
        return [dict(item) for item in output] if isinstance(output, list) else []
    if shape_value == "anthropic_messages":
        content = payload.get("content")
        if not isinstance(content, list):
            return []
        return [{"role": "assistant", "content": [dict(block) for block in content]}]
    return []
