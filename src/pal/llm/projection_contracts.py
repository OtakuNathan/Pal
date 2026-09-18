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
    "PreparedRequest",
]


class ProjectionContractError(ValueError):
    """A projection contract was violated at construction time."""


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
    """

    attempt: AttemptKey
    calls: tuple[ToolCallRecord, ...]
    results: tuple[ToolResultRecord, ...]
    continuation: NativeContinuation

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptKey):
            raise ProjectionContractError("closed round needs an AttemptKey")
        if not isinstance(self.calls, tuple) or not isinstance(self.results, tuple):
            raise ProjectionContractError("closed round items must be tuples")
        if not isinstance(self.continuation, NativeContinuation):
            raise ProjectionContractError("closed round needs a NativeContinuation")
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
            if material.origin != self.attempt:
                raise ProjectionContractError(
                    "native material belongs to a different attempt/owner/scope"
                )
            if tuple(material.call_ids) != call_ids:
                raise ProjectionContractError("stale native call inventory")
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
        if not isinstance(item, Mapping):
            continue
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
    return calls, results


def _validate_sendable_payload(payload: Mapping[str, Any]) -> None:
    """A sendable request cannot carry pending (unanswered) tool calls.

    A checksum proves integrity of bytes, not legality of content: a payload
    with dangling tool_calls still digests fine (review R7).  Every tool call
    in a request the transport may send must already be paired with its
    result — pending calls live in DraftRound, never on the wire.
    """

    calls, results = _payload_tool_inventories(payload)
    pending = sorted(calls - results)
    if pending:
        raise ProjectionContractError(
            f"prepared payload carries pending tool calls without results: {pending}"
        )


@dataclass(frozen=True)
class PreparedRequest:
    """Immutable, tamper-evident request snapshot for one attempt.

    Transport accepts only PreparedRequest instances: no DraftRound or
    hand-assembled mutable payloads (PLAN §4.1).  The payload is canonical
    JSON; the digest is checked at construction so mutation is detectable.
    """

    attempt: AttemptKey
    base_cursor: HistoryCursor
    payload_json: str
    payload_digest: str

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

    @classmethod
    def build(
        cls,
        attempt: AttemptKey,
        base_cursor: HistoryCursor,
        payload: Mapping[str, Any],
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
        )
