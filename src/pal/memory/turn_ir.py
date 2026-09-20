from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import freeze_json_mapping, thaw_json
from pal.memory.continuity import ANCHOR_KEY, Continuity

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from pal.memory.context_view import L1ContextView

from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
    MessageState,
    TextPartIR,
)


class L1TurnState(StrEnum):
    ACTIVE = "active"
    SETTLED = "settled"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"


class L1TurnProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class L1TurnIR:
    turn_id: str
    messages: tuple[LLMMessageIR, ...]
    state: L1TurnState = L1TurnState.ACTIVE
    revision: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.turn_id or "").strip():
            raise ValueError("L1 turn_id must be non-empty")
        object.__setattr__(self, "state", L1TurnState(self.state))
        if int(self.revision) < 0:
            raise ValueError("L1 turn revision must be non-negative")
        object.__setattr__(self, "revision", int(self.revision))
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))
        _validate_protocol(self.messages, allow_pending=self.state == L1TurnState.ACTIVE)
        if self.state != L1TurnState.ACTIVE:
            for message in self.messages:
                if message.state == MessageState.IN_PROGRESS:
                    raise L1TurnProtocolError("settled L1 turn contains an in-progress message")
                if message.reasoning_text:
                    raise L1TurnProtocolError(
                        "settled L1 turn retains provider-neutral reasoning parts; "
                        "wire replay envelopes are allowed and stay frozen"
                    )

    @classmethod
    def begin(
        cls,
        turn_id: str,
        *,
        user_text: str = "",
        user_message: LLMMessageIR | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "L1TurnIR":
        if user_message is not None and str(user_text or ""):
            raise ValueError("provide user_message or user_text, not both")
        if user_message is not None and user_message.role != MessageRole.USER:
            raise ValueError("initial L1 message must have user role")
        messages: tuple[LLMMessageIR, ...] = ()
        if user_message is not None:
            messages = (user_message,)
        elif str(user_text or ""):
            messages = (
                LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(TextPartIR(str(user_text)),),
                    semantic_kind="user_request",
                ),
            )
        return cls(turn_id=str(turn_id), messages=messages, metadata=dict(metadata or {}))

    @property
    def pending_call_ids(self) -> frozenset[str]:
        calls, results = _protocol_ids(self.messages)
        return frozenset(calls - results)

    @property
    def semantic_delta_seen(self) -> bool:
        return any(
            message.role == MessageRole.ASSISTANT
            and bool(message.parts)
            for message in self.messages
        )

    def append(self, message: LLMMessageIR) -> "L1TurnIR":
        self._require_active()
        return replace(self, messages=(*self.messages, message), revision=self.revision + 1)

    def append_user_once(self, message: LLMMessageIR) -> "L1TurnIR":
        """Idempotently append one user message by its stable message ID."""

        self._require_active()
        if message.role != MessageRole.USER:
            raise L1TurnProtocolError("only user messages can use append_user_once")
        matches = [item for item in self.messages if item.message_id == message.message_id]
        if len(matches) > 1:
            raise L1TurnProtocolError("user message id is duplicated in L1")
        if matches:
            if matches[0] != message:
                raise L1TurnProtocolError(
                    "user message id already belongs to different L1 content"
                )
            return self
        return replace(self, messages=(*self.messages, message), revision=self.revision + 1)

    def append_user_contexts(self, messages: tuple[LLMMessageIR, ...], *,
                             coverage_namespace: str = "", coverage: dict[str, Any] | None = None) -> "L1TurnIR":
        """Atomically append idempotent user-role context messages."""

        self._require_active()
        if coverage_namespace and self.pending_call_ids:
            raise L1TurnProtocolError("context requires a closed tool batch")
        updated = self
        for message in tuple(messages):
            updated = updated.append_user_once(message)
        if coverage_namespace:
            metadata = thaw_json(updated.metadata)
            namespaces = metadata.setdefault("observation_coverage", {})
            if namespaces.get(coverage_namespace) != coverage:
                namespaces[coverage_namespace] = coverage
                updated = replace(updated, metadata=metadata, revision=updated.revision + 1)
        return updated

    def append_prompt_contexts(self, messages: tuple[LLMMessageIR, ...], state: dict[str, Any]) -> "L1TurnIR":
        """Commit immutable context records and coverage in one L1 revision."""
        self._require_active()
        STATE_KEY, CONTEXT_KIND = "prompt_context_state", "pal_prompt_context"
        existing = {m.message_id: m for m in self.messages}
        additions = []
        for message in messages:
            if message.semantic_kind != CONTEXT_KIND or message.role not in {MessageRole.USER, MessageRole.DEVELOPER}:
                raise L1TurnProtocolError("invalid prompt context record")
            old = existing.get(message.message_id)
            if old is not None:
                if old != message:
                    raise L1TurnProtocolError("context identity reused with different content")
                continue
            existing[message.message_id] = message
            additions.append(message)
        metadata = {**thaw_json(self.metadata), STATE_KEY: state}
        if not additions and metadata == thaw_json(self.metadata):
            return self
        if self.pending_call_ids:
            raise L1TurnProtocolError("context requires a closed tool batch")
        return replace(self, messages=(*self.messages, *additions), metadata=metadata,
                       revision=self.revision + 1)

    def upsert_assistant(self, message: LLMMessageIR) -> "L1TurnIR":
        self._require_active()
        if message.role != MessageRole.ASSISTANT:
            raise L1TurnProtocolError("only assistant messages can be streamed into L1")
        messages = list(self.messages)
        matches = [index for index, item in enumerate(messages) if item.message_id == message.message_id]
        if len(matches) > 1:
            raise L1TurnProtocolError("assistant message id is duplicated in L1")
        if matches:
            index = matches[0]
            if messages[index].role != MessageRole.ASSISTANT:
                raise L1TurnProtocolError("message id belongs to a non-assistant message")
            messages[index] = message
        else:
            messages.append(message)
        return replace(self, messages=tuple(messages), revision=self.revision + 1)

    def discard_assistant(self, message_id: str) -> "L1TurnIR":
        """Discard one unconsumed assistant response from an active turn.

        This is used when a provider reports that the response was truncated.
        No tool result can have consumed the response yet; rejecting a response
        after any of its tool calls were consumed would break the L1 protocol.
        """

        self._require_active()
        normalized = str(message_id or "").strip()
        if not normalized:
            raise L1TurnProtocolError("assistant message id is required")
        matches = [
            message
            for message in self.messages
            if message.role == MessageRole.ASSISTANT
            and str(message.message_id or "") == normalized
        ]
        if len(matches) != 1:
            raise L1TurnProtocolError(
                f"assistant message id must identify exactly one L1 message: {normalized}"
            )
        discarded_calls = {
            part.call_id
            for part in matches[0].parts
            if isinstance(part, ToolCallIR)
        }
        _, result_ids = _protocol_ids(self.messages)
        consumed = discarded_calls & result_ids
        if consumed:
            raise L1TurnProtocolError(
                f"cannot discard assistant response with consumed tool calls: {sorted(consumed)}"
            )
        messages = tuple(
            message
            for message in self.messages
            if not (
                message.role == MessageRole.ASSISTANT
                and str(message.message_id or "") == normalized
            )
        )
        return replace(self, messages=messages, revision=self.revision + 1)

    def append_tool_result(self, result: ToolResultIR) -> "L1TurnIR":
        self._require_active()
        if result.call_id not in self.pending_call_ids:
            raise L1TurnProtocolError(
                f"late, duplicate, or unknown tool result rejected: {result.call_id}"
            )
        return self.append(
            LLMMessageIR(
                role=MessageRole.TOOL,
                parts=(result,),
                semantic_kind="tool_result",
            )
        )

    def settle(self, *, keep_prefix: int = 0) -> "L1TurnIR":
        """Close the turn, optionally freezing a cut-covered prefix verbatim.

        ``keep_prefix`` head messages keep their exact IR objects — no
        reasoning strip, no closure rewrite: once the root's cut covers them
        they are immutable left-span content and must survive R-side
        settlement byte-for-byte (F2).
        """
        self._require_active()
        if self.pending_call_ids:
            raise L1TurnProtocolError("cannot settle L1 turn with unresolved tool calls")
        frozen = max(0, min(int(keep_prefix), len(self.messages)))
        messages = _ensure_assistant_closure(
            (*self.messages[:frozen],
             *(_close_message(message) for message in self.messages[frozen:]))
        )
        return replace(
            self,
            messages=messages,
            state=L1TurnState.SETTLED,
            revision=self.revision + 1,
        )

    def interrupt(self, *, reason: str = "") -> "L1TurnIR":
        return self._close_incomplete(L1TurnState.INTERRUPTED, reason=reason)

    def abort(self, *, reason: str = "") -> "L1TurnIR":
        return self._close_incomplete(L1TurnState.ABORTED, reason=reason)

    def _close_incomplete(self, state: L1TurnState, *, reason: str) -> "L1TurnIR":
        self._require_active()
        pending = self.pending_call_ids
        closed: list[LLMMessageIR] = []
        for message in self.messages:
            if message.role == MessageRole.ASSISTANT:
                parts = tuple(
                    part
                    for part in message.parts
                    if not isinstance(part, ToolCallIR) or part.call_id not in pending
                )
                if not parts:
                    continue
                protocol_changed = parts != message.parts
                message = replace(
                    message,
                    parts=parts,
                    # Pruned protocol content must not survive in the wire
                    # replay envelope: same-endpoint encoders prefer replay
                    # over parts, so a stale envelope would re-emit the pruned
                    # dangling call onto the wire. Re-encode pruned messages
                    # from their repaired parts instead.
                    replay=None if protocol_changed else message.replay,
                    semantic_kind=(
                        "assistant_reply"
                        if not any(isinstance(part, ToolCallIR) for part in parts)
                        else message.semantic_kind
                    ),
                )
            closed.append(_close_message(message))
        metadata = thaw_json(self.metadata)
        if reason:
            metadata["settlement_reason"] = str(reason)
        return replace(
            self,
            messages=_ensure_assistant_closure(tuple(closed)),
            state=state,
            revision=self.revision + 1,
            metadata=metadata,
        )

    def _require_active(self) -> None:
        if self.state != L1TurnState.ACTIVE:
            raise L1TurnProtocolError(
                f"L1 turn {self.turn_id} is already {self.state.value}; late update rejected"
            )


class _ActiveRound:
    """L1-owned streaming record; frozen predecessors are never visited by update()."""

    def __init__(self, base: L1TurnIR, message: LLMMessageIR) -> None:
        base._require_active()
        matches = [i for i, item in enumerate(base.messages) if item.message_id == message.message_id]
        if matches and (len(matches) != 1 or matches[0] != len(base.messages) - 1
                        or base.messages[-1].role != MessageRole.ASSISTANT):
            raise L1TurnProtocolError("cannot stream into a frozen round or non-assistant record")
        self.base = base
        self.prefix = base.messages[:-1] if matches else base.messages
        self.calls, results = _protocol_ids(self.prefix)
        if self.calls != results:
            raise L1TurnProtocolError("cannot start a round with unresolved tool calls")
        self.message = None
        self.revision = base.revision
        self.cached = None
        self.update(message)

    def update(self, message: LLMMessageIR) -> None:
        if message.role != MessageRole.ASSISTANT:
            raise L1TurnProtocolError("only assistant messages can be streamed into L1")
        if self.message is not None and message.message_id != self.message.message_id:
            raise L1TurnProtocolError("stream changed round identity")
        calls, results = _protocol_ids((message,))
        if calls & self.calls or results:
            raise L1TurnProtocolError("duplicate tool call or tool result inside assistant stream")
        self.message = message
        self.revision += 1
        self.cached = None

    def snapshot(self) -> L1TurnIR:
        if self.cached is None:
            self.cached = replace(self.base, messages=(*self.prefix, self.message), revision=self.revision)
        return self.cached


class L1TurnStore:
    """L1 owns streaming rounds; immutable history is indexed only for a request."""

    def __init__(self, turns: Iterable[L1TurnIR] = ()) -> None:
        self._turns = []
        self._positions = {}
        self._rounds: dict[str, _ActiveRound] = {}
        self._view_sources = None
        self._view = None
        self.continuity: Continuity | None = None
        self._summary_turn_id = ""
        self.replace_all(turns)

    def _invalidate_view(self) -> None:
        self._view_sources = None
        self._view = None

    @property
    def turns(self) -> tuple[L1TurnIR, ...]:
        return tuple(self.get(turn.turn_id) for turn in self._turns)

    def append(self, turn: L1TurnIR) -> None:
        if turn.turn_id in self._positions:
            raise L1TurnProtocolError(f"L1 turn already exists: {turn.turn_id}")
        continuity = self.continuity or self._read_continuity(turn)
        self._positions[turn.turn_id] = len(self._turns)
        self._turns.append(turn)
        if self.continuity is None:
            self.continuity = continuity
            if self.continuity is not None:
                self._summary_turn_id = turn.turn_id
        self._invalidate_view()

    @staticmethod
    def _read_continuity(turn: L1TurnIR) -> Continuity | None:
        message = next((m for m in turn.messages
                        if m.semantic_kind == "runtime_context_summary" and m.text.strip()), None)
        return Continuity.from_message(message, turn.metadata) if message is not None else None

    def project_continuity(self, messages: list[LLMMessageIR]) -> list[LLMMessageIR]:
        continuity = self.continuity
        if continuity is None:
            return messages
        if not continuity.anchor:
            from pal.memory.continuity import is_summary_projection
            visible = {m.message_id for m in messages}
            # Bind once to durable input, never to a transient compiler message.
            anchor = next((m.message_id for turn in self.turns for m in turn.messages
                           if m.message_id in visible and m.role == MessageRole.USER
                           and m.semantic_kind != "pal_prompt_context" and not is_summary_projection(m)),
                          continuity.standalone_id)
            source = self.get(self._summary_turn_id)
            self.replace(replace(source, revision=source.revision + 1,
                                 metadata={**source.metadata, ANCHOR_KEY: anchor}))
            continuity = self.continuity
        return continuity.project(messages)

    def begin(self, turn_id: str, *, user_text: str = "", user_message: LLMMessageIR | None = None,
              metadata: Mapping[str, Any] | None = None) -> L1TurnIR:
        turn = L1TurnIR.begin(str(turn_id or "").strip(), user_text=user_text,
                              user_message=user_message, metadata=metadata)
        self.append(turn)
        return turn

    def get(self, turn_id: str) -> L1TurnIR | None:
        key = str(turn_id or "").strip()
        current = self._rounds.get(key)
        if current is not None:
            return current.snapshot()
        position = self._positions.get(key)
        return self._turns[position] if position is not None else None

    def require_active(self, turn_id: str) -> L1TurnIR:
        turn = self.get(turn_id)
        if turn is None:
            raise L1TurnProtocolError(f"unknown L1 turn: {turn_id}")
        turn._require_active()
        return turn

    def stream_assistant(self, turn_id: str, message: LLMMessageIR) -> None:
        key = str(turn_id or "").strip()
        current = self._rounds.get(key)
        if current is not None and current.message.message_id == message.message_id:
            current.update(message)
        else:
            # Starting a new round is a snapshot boundary, not a per-fragment operation.
            base = self.require_active(key)
            replacement = _ActiveRound(base, message)
            self._turns[self._positions[key]] = base
            self._rounds[key] = replacement
        self._invalidate_view()

    def message(self, turn_id: str, message_id: str) -> LLMMessageIR | None:
        current = self._rounds.get(turn_id)
        if current is not None and current.message.message_id == message_id:
            return current.message
        turn = self.get(turn_id)
        return next((m for m in turn.messages if m.message_id == message_id), None) if turn else None

    def replace(self, turn: L1TurnIR) -> None:
        current = self.get(turn.turn_id)
        if current is None:
            raise L1TurnProtocolError(f"unknown L1 turn: {turn.turn_id}")
        if turn.revision <= current.revision:
            raise L1TurnProtocolError("L1 replacement did not advance revision")
        self.restore_turn(turn, expected=current)

    def restore_turn(self, turn: L1TurnIR, *, expected: L1TurnIR) -> None:
        """Snapshot/rollback boundary: install records and discard derived live state together."""
        if self.get(turn.turn_id) is not expected:
            raise L1TurnProtocolError("L1 changed during rollback")
        continuity = self._read_continuity(turn) if turn.turn_id == self._summary_turn_id else self.continuity
        self._turns[self._positions[turn.turn_id]] = turn
        self.continuity = continuity
        self._rounds.pop(turn.turn_id, None)
        self._invalidate_view()

    def has_open_round(self, turn_id: str) -> bool:
        """True while an assistant streaming round is live for this turn.

        Round-safety evidence for compaction admission: an open round means
        the provider stream or its decoder bookkeeping has not closed yet.
        """
        return turn_id in self._rounds

    def replace_all(self, turns: Iterable[L1TurnIR]) -> None:
        values = list(turns)
        positions = {turn.turn_id: i for i, turn in enumerate(values)}
        if len(positions) != len(values):
            raise L1TurnProtocolError("duplicate L1 turn id")
        summary_turn_id, continuity = "", None
        for turn in values:
            continuity = self._read_continuity(turn)
            if continuity is not None:
                summary_turn_id = turn.turn_id
                break
        self._turns, self._positions = values, positions
        self._summary_turn_id, self.continuity = summary_turn_id, continuity
        self._rounds.clear()
        self._invalidate_view()

    def context_view(self, active_turn_id: str, settled_turns: Iterable[L1TurnIR] = ()) -> "L1ContextView":
        from pal.memory.context_view import L1ContextView, TurnView, projected_messages
        selected = tuple(settled_turns)
        active = self.get(active_turn_id)
        sources = (*selected, *((active,) if active is not None else ()))
        old = self._view_sources
        if old is not None and old[0] == active_turn_id and len(old[1]) == len(sources) and all(
            left is right for left, right in zip(old[1], sources)
        ):
            return self._view
        views, coverage = {}, {}
        for turn in sources:
            messages = projected_messages(turn, settled=turn is not active)
            calls = {part.call_id: m.message_id for m in messages for part in m.parts
                     if isinstance(part, ToolResultIR)}
            views[turn.turn_id] = TurnView.build(messages, calls)
            for namespace, value in turn.metadata.get("observation_coverage", {}).items():
                for key, proof in value.get("states", {}).items():
                    coverage.setdefault((namespace, key), []).append((turn.turn_id, proof))
        self._view = L1ContextView(views, {key: tuple(proofs) for key, proofs in coverage.items()})
        self._view_sources = (active_turn_id, sources)
        return self._view

    def clear(self) -> None:
        self.replace_all(())


def source_stamp_for_turns(turns: Iterable["L1TurnIR"]) -> str:
    """Owner-issued digest over the ordered L1 turn identities.

    Capture and install verification share this function so a caller can
    never substitute a self-reported block count for the real stamp.
    """
    import hashlib

    parts: list[str] = []
    for turn in turns:
        state = str(getattr(turn.state, "value", turn.state) or "")
        message_ids = ";".join(
            str(message.message_id or "") for message in turn.messages
        )
        parts.append(
            f"{turn.turn_id}:{state}:{int(turn.revision)}:"
            f"{len(turn.messages)}:{message_ids}"
        )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def left_span_stamp(turns: Iterable["L1TurnIR"]) -> str:
    """Owner-issued digest over immutable left-span message identity (F2).

    Unlike :func:`source_stamp_for_turns` (whole-history identity including
    per-turn state/revision), this basis is invariant to legal right-side
    growth inside the boundary turn — revision bumps and R-side settlement
    cannot invalidate a captured left — while any same-id change (parts,
    message state, semantic kind, replay envelope, delivery metadata)
    still rewrites the digest.  Whole-turn state/revision stay excluded so
    legal R growth never re-enters the identity.
    """
    import hashlib

    lines: list[str] = []
    for turn in turns:
        lines.append(turn.turn_id)
        for message in turn.messages:
            content = hashlib.sha256(
                f"{message.role}|{message.state}|{message.parts!r}"
                f"|{message.semantic_kind}"
                f"|{message.prompt_region}|{message.replay!r}"
                f"|{message.metadata!r}".encode("utf-8")
            ).hexdigest()
            lines.append(f"{message.message_id}:{content}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _close_message(message: LLMMessageIR) -> LLMMessageIR:
    if message.role == MessageRole.ASSISTANT:
        # Keep the wire replay envelope across settlement: same-endpoint replays
        # stay byte-identical so the prompt-cache prefix survives turn boundaries.
        # Provider-neutral reasoning parts are still stripped: they are the
        # cross-endpoint fallback projection, which must not leak reasoning.
        closed = message.retire_reasoning()
        return replace(closed, replay=message.replay)
    return replace(
        message,
        state=MessageState.COMPLETE,
        replay=None,
    )


def _ensure_assistant_closure(
    messages: tuple[LLMMessageIR, ...],
) -> tuple[LLMMessageIR, ...]:
    """Keep closed tool protocol legal for providers requiring role alternation."""

    if not messages or messages[-1].role != MessageRole.TOOL:
        return messages
    return (
        *messages,
        LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(
                TextPartIR(
                    "Tool interaction closed without a further assistant reply."
                ),
            ),
            semantic_kind="runtime_generated_closure",
            metadata={"pal_authored": True},
        ),
    )


def _protocol_ids(messages: tuple[LLMMessageIR, ...]) -> tuple[set[str], set[str]]:
    calls: set[str] = set()
    results: set[str] = set()
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallIR):
                if part.call_id in calls:
                    raise L1TurnProtocolError(f"duplicate tool call id: {part.call_id}")
                calls.add(part.call_id)
            elif isinstance(part, ToolResultIR):
                if part.call_id in results:
                    raise L1TurnProtocolError(f"duplicate tool result id: {part.call_id}")
                results.add(part.call_id)
    return calls, results


def _validate_protocol(messages: tuple[LLMMessageIR, ...], *, allow_pending: bool) -> None:
    calls, results = _protocol_ids(messages)
    unknown = results - calls
    if unknown:
        raise L1TurnProtocolError(f"orphan tool results in L1: {sorted(unknown)}")
    if not allow_pending and calls != results:
        raise L1TurnProtocolError("settled L1 tool protocol is not closed")
