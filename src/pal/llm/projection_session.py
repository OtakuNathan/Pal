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
from pal.shared.json_values import freeze_json_mapping
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR

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
        # Amortized assembled prefix of all frozen chunk items; extended on
        # commit only, never rebuilt per prepare (PLAN §11: no per-round
        # deepcopy of old chunks).  Private mutable dicts: the public chunk
        # view holds deep-frozen snapshots (review R7).
        self._prefix_items: list[dict] = []
        # Unfrozen wire tail kept across rounds (review F2): trailing items
        # trimmed from a commit (e.g. Anthropic user-role tool results) stay
        # session-owned and are re-injected into every prepare until a later
        # commit freezes past them.  Semantic acceptance and wire freezing
        # are different coordinates.
        self._pending_wire_tail: list[dict] = []
        # Top-level system parts hoisted from past rounds' tail-head
        # system/developer messages (review G2 persistence).  Anthropic
        # renders request-head content outside the container, so committed
        # chunks cannot carry it; the session re-merges these parts into
        # every request after the fresh shell preamble, keeping the
        # incremental lineage equal to whole-history encoding.
        self._committed_head_system: list[dict] = []
        self._owner_fence = 0
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
        # The owner fence survives: it counts worker ownership generations,
        # not endpoint bindings (review R7).
        self.native_by_attempt = {}
        self.chunks = ()
        self._frontier_item_count = 0
        self._committed_attempts = {}
        self.frontier = HistoryCursor.initial()
        self._prefix_items = []
        self._pending_wire_tail = []
        self._committed_head_system = []
        self._active = None

    def retire(self) -> None:
        self.retired = True
        self.native_by_attempt = {}
        self.chunks = ()
        self._prefix_items = []
        self._pending_wire_tail = []
        self._committed_head_system = []
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
        if attempt.owner_fence.fence < self._owner_fence:
            # Owner fences move forward only: a stale worker cannot reopen
            # rounds after a reown (review R7).
            raise ProjectionSessionError(
                "attempt owner fence regresses below the session's current fence"
            )
        self._owner_fence = attempt.owner_fence.fence
        self._active = _ActiveRound(attempt=attempt, requires_native=requires_native)

    def close_round(self) -> AttemptKey:
        """Close the in-flight round without committing (cancel path)."""

        if self._active is None:
            raise ProjectionSessionError("no open round")
        attempt = self._active.attempt
        self._active = None
        return attempt

    def observe_commit(
        self,
        receipt: HistoryCommitReceipt,
        *,
        accepted_messages: Sequence[LLMMessageIR] = (),
    ) -> None:
        """Advance the frontier with a trusted joint commit (idempotent).

        The sealed chunk must contain what this round actually ACCEPTED, not
        merely a re-freeze of the request input (review R3).  Materialization
        sources, in order:

        1. attached native material (when ``receipt.native_committed``) — the
           provider-native assistant wire items, byte-true;
        2. ``accepted_messages`` — IR blocks (e.g. repaired tool results)
           encoded through the shape codec.

        Both may contribute in one commit (native assistant turn + IR tool
        results).  Neither source alone may be replaced by the caller merely
        asserting ``native_committed=True`` (review R4).
        """

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
        self._active.prepared_items.extend(materialized)
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
        unfrozen_suffix: list[dict] = []
        if self.binding.wire_shape.value == "anthropic_messages":
            while items and isinstance(items[-1], dict) and items[-1].get("role") == "user":
                unfrozen_suffix.insert(0, items[-1])
                items = items[:-1]
                frozen_item_count -= 1
        # The trimmed suffix stays session-owned instead of vanishing (review
        # F2): semantic coverage moved past it, but the wire has not frozen
        # it.  Every later prepare re-injects it until a commit freezes past
        # it — callers never re-supply already-accepted results by hand.
        self._pending_wire_tail = [dict(item) for item in unfrozen_suffix]
        if not items:
            raise ProjectionSessionError(
                "round produced no prefix-stable items to freeze"
            )
        chunk = ProjectionChunk(
            round_attempt_id=receipt.attempt.attempt_id,
            cursor_before=self.frontier,
            cursor_after=receipt.append.after,
            # Deep-frozen public snapshot: mutating a committed chunk raises
            # instead of silently rewriting later requests (review R7).
            items=tuple(freeze_json_mapping(item) for item in items),
            prefix_digest=receipt.append.after.prefix_digest,
        )
        self.chunks = (*self.chunks, chunk)
        # The private amortized prefix keeps the mutable dicts; it is never
        # exposed and shares nothing with the frozen chunk snapshot above.
        self._prefix_items.extend(items)
        self._committed_attempts[receipt.attempt.attempt_id] = receipt
        # Request-head content hoisted to top-level system this round becomes
        # session-owned exactly like the wire tail (review G2 persistence):
        # the frozen chunk cannot carry it, but later requests must keep it.
        self._committed_head_system.extend(
            dict(part) for part in self._active.prepared_head_system
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
        """Drop an open round without sealing (late/failed response)."""

        _ = reason
        if self._active is None or self._active.attempt.attempt_id != attempt_id:
            raise ProjectionSessionError("no matching open round to reject")
        self._active = None

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
        context = ShapeContext(
            wire_shape=self.binding.wire_shape,
            endpoint_id=self.binding.endpoint_id,
            model_id=self.binding.model_id,
        )
        container = _ITEM_CONTAINER_KEY.get(self.binding.wire_shape.value, "input")
        shell = request_shell or LLMRequestIR(
            messages=(),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        # 1) Shell envelope, encoded verbatim each prepare (small, stable).
        #    Even a message-less shell (pure policy/tools change) produces
        #    envelope fields; the codec's empty-request fallback items live
        #    in the container and are simply discarded here.
        shell_fields: dict[str, Any] = {}
        preamble_items: list[dict] = []
        encoded_shell = codec.encode(shell, context)
        shell_payload = dict(encoded_shell.payload)
        shell_fields = {
            key: value for key, value in shell_payload.items() if key != container
        }
        preamble_only = tuple(
            message
            for message in shell.messages
            if message.role.value in self._PREAMBLE_ROLES
        )
        if preamble_only:
            encoded_preamble = codec.encode(
                replace(shell, messages=preamble_only), context
            )
            # Only wire items actually mapped to a preamble message (via the
            # codec's own spans) enter the request.  The codec's empty-array
            # fallback ("Continue.") has no span and is excluded here.
            spanned_indexes: set[int] = set()
            for span in encoded_preamble.message_spans:
                for path in span.wire_item_paths:
                    if len(path) >= 2 and path[0] == container and isinstance(path[1], int):
                        spanned_indexes.add(path[1])
            container_items = list(
                dict(encoded_preamble.payload).get(container) or []
            )
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
        else:
            tail_items = []
        # Conversation-span items only (review G1): the preamble NEVER enters
        # the frozen conversation stream, so commit accounting over
        # prepared_items uses conversation coordinates exclusively — a
        # request-with-preamble array can no longer be sliced with a
        # conversation-only cursor, and a committed chunk can no longer
        # freeze the preamble into the prefix.  Preamble items are re-injected
        # fresh at assembly time below, honoring per-request shell budgets.
        conversation_items: list[dict] = [
            *self._prefix_items,
            *(dict(item) for item in self._pending_wire_tail),
            *tail_items,
        ]
        # Anthropic merges adjacent user-role wire messages (source-verified
        # _append_message behavior), so the pending/tail boundary must merge
        # the same way a whole-history encode would — otherwise assembled
        # requests differ from full encodings by one split user message.
        # Boundary index is in conversation coordinates (review G1).
        if (
            self.binding.wire_shape.value == "anthropic_messages"
            and self._pending_wire_tail
            and tail_items
        ):
            boundary = len(self._prefix_items) + len(self._pending_wire_tail) - 1
            _merged, conversation_items = _merge_anthropic_user_boundary(
                conversation_items, boundary
            )
        self._active.prepared_items = conversation_items
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
        payload: dict[str, Any] = {**shell_fields, container: assembled}
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
            *(dict(part) for part in self._committed_head_system),
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
        return PreparedRequest.build(
            attempt=self._active.attempt,
            base_cursor=view.cursor,
            payload=payload,
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
        if not has_native and closed.calls:
            accepted.append(
                LLMMessageIR(
                    role=MessageRole.ASSISTANT,
                    parts=tuple(
                        ToolCallIR(
                            call.call_id,
                            call.name,
                            json.loads(call.arguments_json),
                        )
                        for call in closed.calls
                    ),
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
        context = ShapeContext(
            wire_shape=self.binding.wire_shape,
            endpoint_id=self.binding.endpoint_id,
            model_id=self.binding.model_id,
        )
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
