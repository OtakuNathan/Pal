"""Construction-time contracts for session-level endpoint projection.

PLAN §4: semantic IR, required native continuation, and derived projection
are separate concerns.  This module owns the immutable identity/proof/native
types and the construction rules that make illegal states unrepresentable:

- identity: LogicalSessionId / EndpointBinding / ProjectionIdentity /
  OwnerFence / AttemptKey
- history: HistoryCursor / AppendReceipt / HistoryCommitReceipt
- rounds: DraftRound / ClosedRound (protocol closure + native association)
- continuation: NativeContinuation (absent / required / optional)
- delivery: PreparedRequest (immutable, tamper-evident payload)

Every domain constraint lives in ``__post_init__`` (controlled construction),
never in scattered caller ``if`` checks.  Validation here covers association
and inventory only: provider signature validity and API acceptance belong to
the continuation policy (PLAN §7), not to these contracts.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import WireShape

__all__ = [
    "ProjectionContractError",
    "LogicalSessionId",
    "EndpointBinding",
    "HistoryCursor",
    "ProjectionIdentity",
    "OwnerFence",
    "AttemptKey",
    "NativeContinuationKind",
    "NativeContinuation",
    "NativeMaterial",
    "ToolCallRecord",
    "ToolOutcome",
    "ToolResultRecord",
    "DraftRound",
    "ClosedRound",
    "AppendReceipt",
    "HistoryCommitReceipt",
    "ProjectionSendReceipt",
    "PreparedRequest",
]


class ProjectionContractError(ValueError):
    """A projection contract was violated at construction time."""


# Distinguishes an absent JSON field from an explicit JSON null (review H3):
# neither is an empty object, and replay keeps the raw field unchanged.
_MISSING = object()


def _require_non_empty(value: str, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProjectionContractError(f"{what} must be a non-empty string")


def _require_non_negative(value: int, what: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectionContractError(f"{what} must be a non-negative integer")


# ---------------------------------------------------------------------------
# Identity layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogicalSessionId:
    """Identity of one logical conversation (resident or a Bunshin role).

    Deliberately NOT the endpoint id, run id, PID, or worktree path: coder and
    verifier sessions on the same endpoint stay distinct scopes (PLAN §8.3).
    """

    scope: str

    def __post_init__(self) -> None:
        _require_non_empty(self.scope, "logical session scope")


@dataclass(frozen=True)
class EndpointBinding:
    """The exact provider surface a projection generation is bound to.

    Endpoint switches change ``ProjectionIdentity.generation``; the binding
    itself carries every field that can invalidate a frozen prefix.
    """

    endpoint_id: str
    model_id: str
    wire_shape: WireShape
    endpoint_spec_revision: str
    continuation_policy_version: str
    config_fingerprint: str

    def __post_init__(self) -> None:
        _require_non_empty(self.endpoint_id, "endpoint id")
        _require_non_empty(self.model_id, "model id")
        if not isinstance(self.wire_shape, WireShape):
            raise ProjectionContractError("wire_shape must be a WireShape")
        _require_non_empty(self.endpoint_spec_revision, "endpoint spec revision")
        _require_non_empty(
            self.continuation_policy_version, "continuation policy version"
        )
        _require_non_empty(self.config_fingerprint, "config fingerprint")


@dataclass(frozen=True)
class ProjectionIdentity:
    """One projection lineage: session + binding + generation.

    Switching endpoint/model raises ``projection_generation``; the semantic
    history epoch is tracked separately by HistoryCursor.
    """

    session: LogicalSessionId
    binding: EndpointBinding
    projection_generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.session, LogicalSessionId):
            raise ProjectionContractError("session must be a LogicalSessionId")
        if not isinstance(self.binding, EndpointBinding):
            raise ProjectionContractError("binding must be an EndpointBinding")
        _require_non_negative(self.projection_generation, "projection generation")


@dataclass(frozen=True)
class OwnerFence:
    """Current writer generation; rejects late events after restart/reown."""

    fence: int

    def __post_init__(self) -> None:
        _require_non_negative(self.fence, "owner fence")


@dataclass(frozen=True)
class AttemptKey:
    """Source identity of one provider attempt within one projection."""

    identity: ProjectionIdentity
    owner_fence: OwnerFence
    attempt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProjectionIdentity):
            raise ProjectionContractError("identity must be a ProjectionIdentity")
        if not isinstance(self.owner_fence, OwnerFence):
            raise ProjectionContractError("owner_fence must be an OwnerFence")
        _require_non_empty(self.attempt_id, "attempt id")


# ---------------------------------------------------------------------------
# History layer
# ---------------------------------------------------------------------------


def _digest_of_empty_prefix() -> str:
    return hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class HistoryCursor:
    """Position in the canonical history: epoch + block sequence + digest.

    ``block_sequence`` counts committed history blocks, not messages; the
    turn revision is NOT a cursor component (PLAN §4.1).
    """

    history_epoch: int = 0
    block_sequence: int = 0
    prefix_digest: str = field(default_factory=_digest_of_empty_prefix)

    def __post_init__(self) -> None:
        _require_non_negative(self.history_epoch, "history epoch")
        _require_non_negative(self.block_sequence, "block sequence")
        _require_non_empty(self.prefix_digest, "prefix digest")

    @classmethod
    def initial(cls) -> "HistoryCursor":
        return cls()


@dataclass(frozen=True)
class AppendReceipt:
    """Proof that committed history grew by append only."""

    before: HistoryCursor
    after: HistoryCursor
    block_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.before, HistoryCursor) or not isinstance(
            self.after, HistoryCursor
        ):
            raise ProjectionContractError("append receipt cursors must be HistoryCursor")
        _require_non_negative(self.block_count, "block count")
        if self.block_count == 0:
            raise ProjectionContractError("append receipt must cover at least one block")
        if self.after.history_epoch != self.before.history_epoch:
            raise ProjectionContractError("append receipt cannot change history epoch")
        if (
            self.before.history_epoch != 0 or self.before.block_sequence != 0
        ) and self.before.prefix_digest == _digest_of_empty_prefix():
            raise ProjectionContractError("non-initial cursor lacks a real digest")
        if self.after.block_sequence != self.before.block_sequence + self.block_count:
            raise ProjectionContractError("append span does not match cursors")

    def verify_against(self, current: HistoryCursor) -> None:
        """Same length is not an append proof (PLAN §5.1)."""

        if current != self.before:
            raise ProjectionContractError(
                "current cursor does not match receipt base; same length is not append proof"
            )


@dataclass(frozen=True)
class HistoryCommitReceipt:
    """Joint semantic/native commit: source cursor -> committed cursor.

    One receipt per accepted attempt; consuming the same receipt twice must
    be a no-op and conflicting receipts must fail (idempotency is enforced by
    the session owner, which keys on the attempt).
    """

    attempt: AttemptKey
    append: AppendReceipt
    closed_call_ids: tuple[str, ...]
    native_committed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptKey):
            raise ProjectionContractError("commit receipt needs an AttemptKey")
        if not isinstance(self.append, AppendReceipt):
            raise ProjectionContractError("commit receipt needs an AppendReceipt")
        if not isinstance(self.closed_call_ids, tuple):
            raise ProjectionContractError("closed call ids must be a tuple")
        if len(set(self.closed_call_ids)) != len(self.closed_call_ids):
            raise ProjectionContractError("duplicate call in commit receipt")
        if any(not str(call_id or "").strip() for call_id in self.closed_call_ids):
            raise ProjectionContractError("empty call id in commit receipt")


@dataclass(frozen=True)
class ProjectionSendReceipt:
    """Proof of what the transport actually did with a prepared projection.

    F2 (review af51d74): only this receipt can authorize the owner's
    observe_commit.  It names the projection attempt, the endpoint that
    actually served the request, and whether the projected payload was the
    encode that went on the wire — a dropped projection (endpoint fallback,
    spec refresh, unsupported invoker) reports ``applied=False`` so the
    owner rejects the round instead of freezing a payload the provider
    never saw.  ``native`` carries the codec-level native candidate
    captured for THIS attempt (F3) so the owner can apply the real
    continuation policy; ``binding`` is the binding the projection was
    prepared against (present whenever a projection was offered).
    """

    attempt_id: str
    resolved_endpoint_id: str
    resolved_model_id: str
    resolved_wire_shape: str
    applied: bool
    detail: str = ""
    binding: "EndpointBinding | None" = None
    native: "NativeCandidate | None" = None

    def __post_init__(self) -> None:
        _require_non_empty(self.attempt_id, "projection attempt id")
        _require_non_empty(self.resolved_endpoint_id, "resolved endpoint id")
        _require_non_empty(self.resolved_model_id, "resolved model id")
        _require_non_empty(self.resolved_wire_shape, "resolved wire shape")
        if self.applied and self.binding is None:
            raise ProjectionContractError(
                "an applied projection receipt must carry its binding"
            )


# ---------------------------------------------------------------------------
# Round layer
# ---------------------------------------------------------------------------


class ToolOutcome(Enum):
    SUCCESS = "success"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        _require_non_empty(self.call_id, "call id")
        _require_non_empty(self.name, "tool name")
        _require_non_empty(self.arguments_json, "arguments json")
        try:
            decoded = json.loads(self.arguments_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectionContractError("tool arguments must be valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ProjectionContractError("tool arguments must be a JSON object")


@dataclass(frozen=True)
class ToolResultRecord:
    call_id: str
    body: str
    outcome: ToolOutcome

    def __post_init__(self) -> None:
        _require_non_empty(self.call_id, "result call id")
        if not isinstance(self.outcome, ToolOutcome):
            raise ProjectionContractError("outcome must be a ToolOutcome")
        if not isinstance(self.body, str):
            raise ProjectionContractError("result body must be a string")


class NativeContinuationKind(Enum):
    ABSENT = "absent"
    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class NativeMaterial:
    """Provider-native continuation material bound to its source attempt.

    ``payload_json`` is an opaque, policy-validated JSON document; the
    contracts only enforce provenance and inventory association.  Native data
    is stored per active binding and destroyed on endpoint switch (PLAN §3.2).
    """

    origin: AttemptKey
    call_ids: tuple[str, ...]
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.origin, AttemptKey):
            raise ProjectionContractError("native material needs a source AttemptKey")
        if not isinstance(self.call_ids, tuple):
            raise ProjectionContractError("native call inventory must be a tuple")
        if len(set(self.call_ids)) != len(self.call_ids):
            raise ProjectionContractError("duplicate native call id")
        _require_non_empty(self.payload_json, "native payload json")


@dataclass(frozen=True)
class NativeContinuation:
    """Discriminated continuation state for one round."""

    kind: NativeContinuationKind
    material: NativeMaterial | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NativeContinuationKind):
            raise ProjectionContractError("continuation kind is required")
        if self.kind is NativeContinuationKind.ABSENT:
            if self.material is not None:
                raise ProjectionContractError("absent continuation cannot carry material")
        elif self.material is None:
            raise ProjectionContractError(
                f"{self.kind.value} continuation requires native material"
            )


@dataclass(frozen=True)
class DraftRound:
    """Work-in-progress round; mirrors the TLA DraftAligned/TypeOK invariants.

    ``results ⊆ started ⊆ calls`` and the native inventory must match the
    semantic call inventory exactly, or a stale envelope could smuggle pruned
    calls back onto the wire.
    """

    attempt: AttemptKey
    calls: tuple[ToolCallRecord, ...]
    started: frozenset[str]
    results: frozenset[str]
    native_calls: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptKey):
            raise ProjectionContractError("draft round needs an AttemptKey")
        if not isinstance(self.calls, tuple):
            raise ProjectionContractError("draft calls must be a tuple")
        for collection, what in (
            (self.started, "started"),
            (self.results, "results"),
            (self.native_calls, "native calls"),
        ):
            if not isinstance(collection, frozenset):
                raise ProjectionContractError(f"draft {what} must be a frozenset")
        call_ids = frozenset(call.call_id for call in self.calls)
        if len(call_ids) != len(self.calls):
            raise ProjectionContractError("duplicate call id in draft round")
        if not self.results <= self.started:
            raise ProjectionContractError("results without a started call")
        if not self.started <= call_ids:
            raise ProjectionContractError("started call outside the call inventory")
        if self.native_calls != call_ids:
            raise ProjectionContractError(
                "draft native inventory must equal the semantic call inventory"
            )


@dataclass(frozen=True)
class ClosedRound:
    """A round whose tool protocol and native material are fully closed.

    ``closed`` means: item order finalized, call/result sets exactly paired,
    no unknown side effects, and (when required) native material bound to the
    same attempt with a matching call sequence (PLAN §4.1/§6).

    ``assistant_texts`` (review H2) carries the repaired round's PRESERVED
    assistant text — the conclusions a legally pruned round must keep beyond
    its tool inventory.  Without this channel a no-native repair could only
    materialize calls/results, and the accepted text would never reach later
    requests: the frontier-based tail can only re-supply what it was handed.
    One representation per assistant contribution still holds — when native
    material carries the assistant turn, semantic texts are refused instead
    of duplicated.  Materialization order is texts first, then calls.
    """

    attempt: AttemptKey
    calls: tuple[ToolCallRecord, ...]
    results: tuple[ToolResultRecord, ...]
    continuation: NativeContinuation
    assistant_texts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptKey):
            raise ProjectionContractError("closed round needs an AttemptKey")
        if not isinstance(self.calls, tuple) or not isinstance(self.results, tuple):
            raise ProjectionContractError("closed round items must be tuples")
        if not isinstance(self.continuation, NativeContinuation):
            raise ProjectionContractError("closed round needs a NativeContinuation")
        if not isinstance(self.assistant_texts, tuple):
            raise ProjectionContractError("assistant texts must be a tuple")
        for text in self.assistant_texts:
            _require_non_empty(text, "assistant text")
        call_ids = tuple(call.call_id for call in self.calls)
        result_ids = tuple(result.call_id for result in self.results)
        if len(set(call_ids)) != len(call_ids):
            raise ProjectionContractError("duplicate call in closed round")
        if len(set(result_ids)) != len(result_ids):
            raise ProjectionContractError("duplicate result in closed round")
        if set(call_ids) != set(result_ids):
            raise ProjectionContractError("unclosed call/result protocol")
        if any(
            result.outcome is ToolOutcome.UNKNOWN for result in self.results
        ):
            raise ProjectionContractError(
                "unknown side effects require reconciliation before closing"
            )
        material = self.continuation.material
        if material is not None:
            if self.assistant_texts:
                raise ProjectionContractError(
                    "assistant texts duplicate the native material's assistant turn"
                )
            if material.origin != self.attempt:
                raise ProjectionContractError(
                    "native material belongs to a different attempt/owner/scope"
                )
            if tuple(material.call_ids) != call_ids:
                raise ProjectionContractError("stale native call inventory")
            # Call-ID equality alone proves nothing about WHAT was called
            # (review G3): the native payload's calls must match the accepted
            # semantic records by ordered id, name, and semantically equal
            # arguments, or the next request would describe a different
            # operation than the accepted one.
            mismatch = _native_call_inventory_mismatch(
                self.attempt.identity.binding.wire_shape.value,
                material.payload_json,
                self.calls,
            )
            if mismatch:
                raise ProjectionContractError(
                    f"native material does not match the accepted call records: {mismatch}"
                )
        if self.continuation.kind is NativeContinuationKind.REQUIRED and material is None:
            raise ProjectionContractError("required native continuation is missing")


# ---------------------------------------------------------------------------
# Delivery layer
# ---------------------------------------------------------------------------


_TOOL_CALL_RESULT_CONTAINERS = ("messages", "input")


def _payload_tool_inventories(payload: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Extract (call_ids, result_ids) from a provider payload for all shapes.

    Recognized pairings:
    - openai_completion: messages[].tool_calls[].id  <->  role:"tool" tool_call_id
    - openai_response:   input[].function_call.call_id <-> function_call_output.call_id
    - anthropic_messages: content[].tool_use.id      <->  content[].tool_result.tool_use_id
    """

    calls: set[str] = set()
    results: set[str] = set()
    container = next(
        (key for key in _TOOL_CALL_RESULT_CONTAINERS if isinstance(payload.get(key), (list, tuple))),
        None,
    )
    if container is None:
        return calls, results
    for item in payload[container]:
        _collect_item_tool_ids(item, calls, results)
    return calls, results


def _collect_item_tool_ids(item: Any, calls: set[str], results: set[str]) -> None:
    if not isinstance(item, Mapping):
        return
    if isinstance(item.get("tool_calls"), (list, tuple)):
        for call in item["tool_calls"]:
            if isinstance(call, Mapping):
                call_id = str(call.get("id") or "").strip()
                if call_id:
                    calls.add(call_id)
    if str(item.get("role") or "") == "tool":
        result_id = str(item.get("tool_call_id") or "").strip()
        if result_id:
            results.add(result_id)
    item_type = str(item.get("type") or "")
    if item_type == "function_call":
        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            calls.add(call_id)
    elif item_type == "function_call_output":
        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            results.add(call_id)
    content = item.get("content")
    if isinstance(content, (list, tuple)):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "tool_use":
                call_id = str(block.get("id") or "").strip()
                if call_id:
                    calls.add(call_id)
            elif block_type == "tool_result":
                call_id = str(block.get("tool_use_id") or "").strip()
                if call_id:
                    results.add(call_id)


def _item_tool_events(item: Any) -> tuple[list[tuple[str, str]], list[str]]:
    """Ordered tool events and placement problems in ONE pass over the item.

    Each event is ``("call"|"result", call_id)``.  Interleaving is preserved
    so the validator can reject a result that precedes its call INSIDE the
    same item (review G4); the previous two-list shape normalized that
    ordering away.  Role-appropriate placement is collected in the same
    pass: content-block ``tool_use`` is legal only inside an assistant
    message, ``tool_result`` only inside a user message, completion
    ``tool_calls`` only on an assistant message.  One allocation-light pass
    keeps the sendable gate linear and cheap on large requests.
    """

    events: list[tuple[str, str]] = []
    problems: list[str] = []
    if not isinstance(item, Mapping):
        return events, problems
    role = str(item.get("role") or "")
    if isinstance(item.get("tool_calls"), (list, tuple)):
        if item["tool_calls"] and role != "assistant":
            problems.append(
                f"tool_calls outside an assistant message (role={role!r})"
            )
        for call in item["tool_calls"]:
            if isinstance(call, Mapping):
                call_id = str(call.get("id") or "").strip()
                if call_id:
                    events.append(("call", call_id))
    if role == "tool":
        result_id = str(item.get("tool_call_id") or "").strip()
        if result_id:
            events.append(("result", result_id))
        else:
            problems.append("tool message has no tool_call_id")
    item_type = str(item.get("type") or "")
    if item_type == "function_call":
        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            events.append(("call", call_id))
    elif item_type == "function_call_output":
        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            events.append(("result", call_id))
    content = item.get("content")
    if isinstance(content, (list, tuple)):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "tool_use":
                if role != "assistant":
                    problems.append(
                        f"tool_use block outside an assistant message (role={role!r})"
                    )
                call_id = str(block.get("id") or "").strip()
                if call_id:
                    events.append(("call", call_id))
            elif block_type == "tool_result":
                if role != "user":
                    problems.append(
                        f"tool_result block outside a user message (role={role!r})"
                    )
                call_id = str(block.get("tool_use_id") or "").strip()
                if call_id:
                    events.append(("result", call_id))
    return events, problems


def _validate_sendable_payload(payload: Mapping[str, Any]) -> None:
    """Validate the ordered tool protocol of a request the transport may send.

    A checksum proves integrity of bytes, not legality of content.  The
    protocol trace is validated linearly, per pending group: a call must
    open its group, a result must consume an open call, one pending group
    per call id at a time, and nothing may remain pending at the end.
    Orphan results, duplicate calls within one group, and results before
    their calls are all rejected (review F4).  Set equality of ids is NOT a
    protocol proof and is not used.
    """

    container = next(
        (key for key in _TOOL_CALL_RESULT_CONTAINERS if isinstance(payload.get(key), (list, tuple))),
        None,
    )
    if container is None:
        return
    open_calls: dict[str, str] = {}
    problems: list[str] = []
    for index, item in enumerate(payload[container]):
        events, placement = _item_tool_events(item)
        if placement:
            problems.extend(f"item[{index}]: {problem}" for problem in placement)
        for kind, call_id in events:
            if kind == "call":
                if call_id in open_calls:
                    problems.append(
                        f"item[{index}]: duplicate call {call_id!r} while its previous "
                        "occurrence is still unanswered"
                    )
                else:
                    open_calls[call_id] = str(index)
            else:
                if call_id not in open_calls:
                    problems.append(
                        f"item[{index}]: orphan result {call_id!r} without an open call "
                        "(missing call, wrong block order, or result precedes its call)"
                    )
                else:
                    del open_calls[call_id]
    for call_id in sorted(open_calls):
        problems.append(f"pending tool call {call_id!r} has no result")
    if problems:
        raise ProjectionContractError(
            "prepared payload violates the tool protocol: " + "; ".join(problems)
        )


# ---------------------------------------------------------------------------
# Native/semantic call compatibility (review G3)
# ---------------------------------------------------------------------------


def _json_values_equal(left: Any, right: Any) -> bool:
    """Structural JSON equality for tool-call arguments.

    ``bool`` never equals a number (Python's ``True == 1`` must not leak into
    wire semantics) while ``int``/``float`` compare by numeric value so a
    provider echoing ``1.0`` for ``1`` stays a compatible continuation.
    """

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left.keys()) != set(right.keys()):
            return False
        return all(_json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _json_values_equal(a, b) for a, b in zip(left, right)
        )
    if type(left) is not type(right):
        return False
    return left == right


def _native_arguments_match(native_args: Any, semantic_json: str) -> bool:
    """Semantic JSON equality between a native argument field and the record.

    An ABSENT or EXPLICIT-NULL native field is never an empty object (review
    H3): replay keeps the raw field unchanged, so validation must not promote
    it to ``{}`` either.  ``bool`` stays distinct from numbers and ``int``/
    ``float`` compare by numeric value (see ``_json_values_equal``).
    """

    if isinstance(native_args, str):
        try:
            native_args = json.loads(native_args)
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(native_args, Mapping):
        return False
    try:
        semantic = json.loads(semantic_json)
    except (TypeError, json.JSONDecodeError):
        return False
    return _json_values_equal(native_args, semantic)


def _native_call_inventory_mismatch(
    shape_value: str,
    payload_json: str,
    calls: tuple[ToolCallRecord, ...],
) -> str:
    """Compare the native payload's calls with the accepted semantic records.

    Returns ``""`` when every native call matches its semantic record by
    ordered id, name, and semantically equal parsed arguments; otherwise a
    human-readable mismatch description.  Signatures and encrypted bytes are
    never mutated to force agreement (review G3): mismatch rejects
    continuity instead.
    """

    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        return f"native payload is not valid JSON: {exc}"
    if not isinstance(payload, Mapping):
        return "native payload is not a JSON object"
    # The argument field's PRESENCE is tracked with a sentinel (review H3):
    # ``.get()`` alone cannot distinguish a missing field from an explicit
    # JSON null, and neither is an empty object.
    native: list[tuple[str, str, Any]] = []
    if shape_value == "openai_completion":
        message = payload.get("message")
        if isinstance(message, Mapping) and isinstance(
            message.get("tool_calls"), (list, tuple)
        ):
            for call in message["tool_calls"]:
                if isinstance(call, Mapping):
                    function = call.get("function")
                    native.append(
                        (
                            str(call.get("id") or ""),
                            str(function.get("name") or "")
                            if isinstance(function, Mapping)
                            else "",
                            function.get("arguments", _MISSING)
                            if isinstance(function, Mapping)
                            else _MISSING,
                        )
                    )
    elif shape_value == "openai_response":
        output = payload.get("output")
        if isinstance(output, (list, tuple)):
            for item in output:
                if (
                    isinstance(item, Mapping)
                    and str(item.get("type") or "") == "function_call"
                ):
                    native.append(
                        (
                            str(item.get("call_id") or ""),
                            str(item.get("name") or ""),
                            item.get("arguments", _MISSING),
                        )
                    )
    elif shape_value == "anthropic_messages":
        content = payload.get("content")
        if isinstance(content, (list, tuple)):
            for block in content:
                if (
                    isinstance(block, Mapping)
                    and str(block.get("type") or "") == "tool_use"
                ):
                    native.append(
                        (
                            str(block.get("id") or ""),
                            str(block.get("name") or ""),
                            block.get("input", _MISSING),
                        )
                    )
    else:
        return f"unsupported wire shape for native call validation: {shape_value!r}"
    if len(native) != len(calls):
        return (
            f"native call count {len(native)} does not match the accepted "
            f"semantic records {len(calls)}"
        )
    # The two OpenAI shapes carry arguments as a JSON STRING; Anthropic
    # carries tool input as a JSON OBJECT (review H3): the raw wire type is
    # validated per shape before semantic comparison, never silently coerced
    # into whichever type happens to compare equal.
    arguments_are_strings = shape_value in ("openai_completion", "openai_response")
    for (native_id, native_name, native_args), call in zip(native, calls):
        if native_id != call.call_id:
            return (
                f"native call order/id {native_id!r} does not match semantic "
                f"{call.call_id!r}"
            )
        if native_name != call.name:
            return (
                f"native call {call.call_id!r} name {native_name!r} does not "
                f"match the accepted name {call.name!r}"
            )
        if native_args is _MISSING:
            return (
                f"native call {call.call_id!r} has no arguments field; a missing "
                "field is not an empty object"
            )
        if native_args is None:
            return (
                f"native call {call.call_id!r} has an explicit null arguments "
                "field; null is not an empty object"
            )
        if arguments_are_strings and not isinstance(native_args, str):
            return (
                f"native call {call.call_id!r} arguments must be a JSON string "
                f"on {shape_value}"
            )
        if not arguments_are_strings and not isinstance(native_args, Mapping):
            return (
                f"native call {call.call_id!r} tool input must be a JSON object "
                f"on {shape_value}"
            )
        if not _native_arguments_match(native_args, call.arguments_json):
            return (
                f"native call {call.call_id!r} arguments do not semantically "
                "match the accepted record"
            )
    return ""


@dataclass(frozen=True)
class PreparedRequest:
    """Immutable, tamper-evident request snapshot for one attempt.

    Transport accepts only PreparedRequest instances: no DraftRound or
    hand-assembled mutable payloads (PLAN §4.1).  The payload is canonical
    JSON; the digest is checked at construction so mutation is detectable.

    W2 (review NEXT_STEPS §3.1): the derived wire contract travels WITH the
    payload — message spans, extra body params, and applied cache
    breakpoints from the same codec encode — so the send seam can consume
    the full EncodedRequest contract without re-encoding.  Assembled normal
    prepares fill these when their seams land (N3 invoker wiring); a
    single-encode prepare (handoff) is always complete.
    """

    attempt: AttemptKey
    base_cursor: HistoryCursor
    payload_json: str
    payload_digest: str
    message_spans: tuple = ()
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    applied_cache_breakpoint_message_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptKey):
            raise ProjectionContractError("prepared request needs an AttemptKey")
        if not isinstance(self.base_cursor, HistoryCursor):
            raise ProjectionContractError("prepared request needs a HistoryCursor")
        _require_non_empty(self.payload_json, "payload json")
        _require_non_empty(self.payload_digest, "payload digest")
        computed = hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()
        if computed != self.payload_digest:
            raise ProjectionContractError("prepared payload digest mismatch")
        # The SAME protocol validation as build(): a correct checksum on an
        # illegal payload must not make it sendable (review F4).  This covers
        # direct construction and deserialization paths.
        try:
            parsed = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectionContractError("prepared payload is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ProjectionContractError("prepared payload must be a JSON object")
        _validate_sendable_payload(parsed)

    @classmethod
    def build(
        cls,
        attempt: AttemptKey,
        base_cursor: HistoryCursor,
        payload: Mapping[str, Any],
        *,
        message_spans: tuple = (),
        extra_body: Mapping[str, Any] | None = None,
        applied_cache_breakpoint_message_ids: tuple[str, ...] = (),
    ) -> "PreparedRequest":
        # Build is the single controlled entry: protocol legality (no pending
        # tool calls) is checked here, not left to caller discipline.
        _validate_sendable_payload(payload)
        payload_json = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return cls(
            attempt=attempt,
            base_cursor=base_cursor,
            payload_json=payload_json,
            payload_digest=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            message_spans=tuple(message_spans),
            extra_body=dict(extra_body or {}),
            applied_cache_breakpoint_message_ids=tuple(
                applied_cache_breakpoint_message_ids
            ),
        )
