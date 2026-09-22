"""Two-segment session history authority (pal-two-segment-v3 PLAN §3, §5).

Single owner over one session's history split into:

- ``L`` — the compressible closed prefix (settled turns, closed round
  prefixes, and the summary seed after an install);
- ``R`` — the work tail (active turns, open groups, latest results).

The underlying :class:`~pal.memory.turn_ir.L1TurnStore` stays the single
physical storage (A03: no mirrored second history).  The cut is a logical
boundary over the store's ordered turns; left/right are derived views.

Compact runs follow the terminal arbitration of PLAN §5:

    RUNNING -> READY(candidate) -> COMMITTED
              |-> FAILED
              |-> CANCELLED

Exactly one terminal wins per run.  A committed install replaces only the
left side in one non-awaiting section and never assigns ``R``; a cancelled
or failed run leaves ``L`` untouched.  ``left_revision`` changes on promote
and replace-left and is *not* a fence for right-side producers; only a
session incarnation change (reset) invalidates them (I09).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from pal.llm.ir import LLMMessageIR, MessageRole, MessageState
from pal.memory.turn_ir import (
    L1TurnIR,
    L1TurnState,
    L1TurnStore,
    _close_message,
    _protocol_ids,
    left_span_stamp,
)
from pal.shared.tool_protocol import ToolResultIR

__all__ = [
    "CutPosition",
    "LeftSnapshot",
    "CompactPhase",
    "CompactRun",
    "CompactOutcome",
    "ProducerToken",
    "HistoryRoot",
    "HistoryRootError",
    "CompactLaneBusy",
    "NoBeneficialCompaction",
    "FrozenLeftDuringCompact",
    "LeftChangedSinceCapture",
    "TerminalClosed",
    "StaleRun",
    "CandidateConflict",
    "StaleProducer",
]

SUMMARY_TURN_ID = "compact-summary"


class HistoryRootError(RuntimeError):
    """Base error carrying identity facts, never a bare ERROR string."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail: dict[str, Any] = dict(detail)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.detail or str(self)})"


class CompactLaneBusy(HistoryRootError):
    """A second compact run cannot open while one is live (I06)."""


class NoBeneficialCompaction(HistoryRootError):
    """L is empty or already the minimal summary seed (L08)."""


class FrozenLeftDuringCompact(HistoryRootError):
    """The cut cannot move while a compact run is live (I04, F05, L07)."""


class LeftChangedSinceCapture(HistoryRootError):
    """L no longer matches the snapshot captured for this run (CAS)."""


class TerminalClosed(HistoryRootError):
    """The run already reached a terminal state; late events hold no power."""

    def __init__(self, message: str, *, run_id: str, phase: str, winner: str) -> None:
        super().__init__(message, run_id=run_id, phase=phase, winner=winner)
        self.run_id = run_id
        self.phase = phase
        self.winner = winner


class StaleRun(HistoryRootError):
    """The referenced run is not the current run (X04)."""


class CandidateConflict(HistoryRootError):
    """A different candidate already won this run's READY slot (C07)."""


class StaleProducer(HistoryRootError):
    """Producer token belongs to an earlier session incarnation (X05)."""


class CompactPhase(StrEnum):
    RUNNING = "running"
    READY = "ready"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (CompactPhase.COMMITTED, CompactPhase.CANCELLED, CompactPhase.FAILED)


@dataclass(frozen=True)
class CutPosition:
    """Logical L/R boundary over the store's ordered turns.

    ``turn_count`` turns are wholly in L; when ``intra_messages > 0`` the
    boundary turn ``turns[turn_count]`` contributes its first
    ``intra_messages`` messages to L (closed-round prefix of an active turn).
    """

    turn_count: int
    intra_messages: int = 0
    revision: int = 0
    cut_id: str = ""

    def advanced(self, *, turn_count: int, intra_messages: int) -> "CutPosition":
        return CutPosition(
            turn_count=turn_count,
            intra_messages=intra_messages,
            revision=self.revision + 1,
            cut_id=f"cut-{uuid4().hex[:12]}",
        )


@dataclass(frozen=True)
class LeftSnapshot:
    """Frozen view of L captured when a compact run opens (I04, I10)."""

    run_id: str
    cut: CutPosition
    turns: tuple[L1TurnIR, ...]
    stamp: str

    def message_ids(self) -> tuple[str, ...]:
        return tuple(message.message_id for turn in self.turns for message in turn.messages)


@dataclass
class CompactRun:
    run_id: str
    reason: str
    parent_turn_id: str
    snapshot: LeftSnapshot
    phase: CompactPhase = CompactPhase.RUNNING
    candidate: Any | None = None
    candidate_id: str = ""
    winner: str = "none"
    terminal_detail: str = ""
    deadline_at: float | None = None
    committed_left_revision: int | None = None

    @property
    def terminal(self) -> bool:
        return self.phase.terminal


@dataclass(frozen=True)
class CompactOutcome:
    run_id: str
    status: str
    left_revision: int
    seed_turn_id: str = ""
    replayed: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ProducerToken:
    """Identity an R producer keeps across left replacement but not reset."""

    incarnation: str
    turn_id: str


def _summary_turn(text: str, payload: Mapping[str, Any] | None = None) -> L1TurnIR:
    from pal.llm.ir import TextPartIR

    return L1TurnIR(
        turn_id=SUMMARY_TURN_ID,
        messages=(
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR(str(text)),),
                semantic_kind="runtime_context_summary",
                metadata=dict(payload or {}),
            ),
        ),
        state=L1TurnState.SETTLED,
    )


def default_summary_builder(candidate: Any) -> L1TurnIR:
    """Render a v2-compatible summary seed from an L2Entry-like candidate."""

    rendered = str(getattr(candidate, "rendered", "") or "").strip()
    summary = str(getattr(candidate, "summary", "") or "").strip()
    text = rendered or summary
    if not text:
        raise ValueError("summary candidate has no rendered or summary text")
    payload = getattr(candidate, "payload", None) or {}
    return _summary_turn(text, payload if isinstance(payload, Mapping) else None)


class HistoryRoot:
    """Single-writer owner of the two-segment history (I01).

    All mutations of L or R go through this object's methods; producers
    only hold tokens and deliver events.  The wrapped store remains the
    one physical history — every existing reader keeps reading it.
    """

    def __init__(
        self,
        store: L1TurnStore | None = None,
        *,
        incarnation: str | None = None,
    ) -> None:
        self.store = store if store is not None else L1TurnStore()
        self._incarnation = str(incarnation or f"inc-{uuid4().hex[:12]}")
        self._cut = CutPosition(turn_count=0, intra_messages=0, revision=0,
                                cut_id=f"cut-{uuid4().hex[:12]}")
        self._run: CompactRun | None = None
        self._runs: dict[str, CompactRun] = {}
        # Left REPLACEMENT generation — bumped only by a committed compact
        # install, never by promote.  Derived projections (e.g. the endpoint
        # projection session) track this to detect staleness without
        # conflating ordinary cut movement with a left replacement.
        self._left_generation = 0

    # ------------------------------------------------------------------
    # Views (derived; the store stays the single physical fact — A03)
    # ------------------------------------------------------------------

    @property
    def incarnation(self) -> str:
        return self._incarnation

    @property
    def cut(self) -> CutPosition:
        return self._cut

    @property
    def left_revision(self) -> int:
        return self._cut.revision

    @property
    def left_generation(self) -> int:
        """Count of committed left replacements (not promote moves)."""
        return self._left_generation

    @property
    def active_run(self) -> CompactRun | None:
        run = self._run
        return run if run is not None and not run.terminal else None

    @property
    def last_run(self) -> CompactRun | None:
        return self._run

    def run_record(self, run_id: str) -> CompactRun | None:
        run = self._runs.get(str(run_id or ""))
        if run is not None:
            return run
        if self._run is not None and self._run.run_id == run_id:
            return self._run
        return None

    def all_turns(self) -> tuple[L1TurnIR, ...]:
        return self.store.turns

    def _ordered_turns(self) -> list[L1TurnIR]:
        return list(self.store.turns)

    def left_turns(self) -> tuple[L1TurnIR, ...]:
        turns = self._ordered_turns()
        whole = turns[: self._cut.turn_count]
        if self._cut.intra_messages:
            boundary = turns[self._cut.turn_count]
            prefix = self._boundary_prefix(boundary, self._cut.intra_messages)
            if prefix is not None:
                whole = [*whole, prefix]
        return tuple(whole)

    def right_turns(self) -> tuple[L1TurnIR, ...]:
        turns = self._ordered_turns()
        if self._cut.intra_messages:
            boundary = turns[self._cut.turn_count]
            suffix = self._boundary_suffix(boundary, self._cut.intra_messages)
            if suffix is not None:
                return (suffix, *turns[self._cut.turn_count + 1:])
            # The intra cut consumed the whole boundary turn.
            return tuple(turns[self._cut.turn_count + 1:])
        return tuple(turns[self._cut.turn_count:])

    @staticmethod
    def _boundary_prefix(boundary: L1TurnIR, count: int) -> L1TurnIR | None:
        if count <= 0 or count > len(boundary.messages):
            return None
        return replace(boundary, messages=boundary.messages[:count])

    @staticmethod
    def _boundary_suffix(boundary: L1TurnIR, count: int) -> L1TurnIR | None:
        if count <= 0 or count >= len(boundary.messages):
            return None
        return replace(boundary, messages=boundary.messages[count:])

    def left_messages(self) -> tuple[LLMMessageIR, ...]:
        return tuple(m for t in self.left_turns() for m in t.messages)

    def right_messages(self) -> tuple[LLMMessageIR, ...]:
        return tuple(m for t in self.right_turns() for m in t.messages)

    def _left_stamp(self) -> str:
        # Binds immutable left content to the owner-issued cut version
        # (review F2: content basis + cut/left version): a stale candidate
        # can never install against a different cut even if the prefix
        # content coincides (e.g. intra==len vs whole-turn coverage).
        return (
            left_span_stamp(self.left_turns())
            + f"@{self._cut.revision}:{self._cut.cut_id}"
        )

    # ------------------------------------------------------------------
    # H02 — graceful-stop owner state (persist/restore)
    # ------------------------------------------------------------------

    def owner_state(self) -> dict[str, Any]:
        """Persistable owner state for a graceful shutdown snapshot (H02).

        The store's turns are snapshotted separately by the memory runtime
        state port; this carries only the authority facts (incarnation, cut,
        left replacement generation) plus a diagnostic record of a compact
        run that was still live at save time — such a run is abandoned, and
        a restored process never resumes or re-runs it.
        """

        active = self.active_run
        return {
            "incarnation": self._incarnation,
            "left_generation": int(self._left_generation),
            "cut": {
                "turn_count": int(self._cut.turn_count),
                "intra_messages": int(self._cut.intra_messages),
                "revision": int(self._cut.revision),
                "cut_id": str(self._cut.cut_id),
            },
            "abandoned_run": (
                {"run_id": active.run_id, "phase": active.phase.value}
                if active is not None
                else None
            ),
        }

    def restore_owner_state(
        self,
        *,
        incarnation: str,
        cut: CutPosition,
        left_generation: int,
    ) -> None:
        """Reinstate persisted owner state over the already-restored store.

        The wrapped store must already hold the persisted turns; the cut is
        validated against them so a corrupted snapshot fails closed (only a
        complete old or new root is ever visible — never a half-installed
        one).  Run bookkeeping is never restored: a run that was live at
        save time was abandoned, and this process starts with a free lane.
        """

        if self.active_run is not None:
            raise HistoryRootError(
                "cannot restore owner state while a compact run is live",
                incarnation=self._incarnation,
            )
        identity = str(incarnation or "").strip()
        cut_identity = str(getattr(cut, "cut_id", "") or "").strip()
        if not identity or not cut_identity:
            raise HistoryRootError(
                "restored owner state is missing identity fields",
                incarnation=identity,
                cut_id=cut_identity,
            )
        turn_count = int(getattr(cut, "turn_count", -1))
        intra_messages = int(getattr(cut, "intra_messages", -1))
        revision = int(getattr(cut, "revision", -1))
        generation = int(left_generation)
        if min(turn_count, intra_messages, revision, generation) < 0:
            raise HistoryRootError(
                "restored owner state has negative counters",
                turn_count=turn_count,
                intra_messages=intra_messages,
                revision=revision,
                left_generation=generation,
            )
        turns = self._ordered_turns()
        total = len(turns)
        if turn_count > total:
            raise HistoryRootError(
                "restored cut exceeds the restored history",
                turn_count=turn_count,
                turn_total=total,
            )
        if intra_messages:
            if turn_count >= total:
                raise HistoryRootError(
                    "restored intra cut has no boundary turn",
                    turn_count=turn_count,
                    turn_total=total,
                )
            boundary_messages = len(turns[turn_count].messages)
            if intra_messages > boundary_messages:
                raise HistoryRootError(
                    "restored intra cut splits messages that never existed",
                    intra_messages=intra_messages,
                    boundary_messages=boundary_messages,
                )
        self._incarnation = identity
        self._cut = CutPosition(
            turn_count=turn_count,
            intra_messages=intra_messages,
            revision=revision,
            cut_id=cut_identity,
        )
        self._left_generation = generation


    # ------------------------------------------------------------------
    # R production — owner-only writes (§3.2)
    # ------------------------------------------------------------------

    def producer_token(self, turn_id: str) -> ProducerToken:
        return ProducerToken(incarnation=self._incarnation, turn_id=str(turn_id))

    def begin_right_turn(self, turn_id: str, *, user_text: str = "",
                         user_message: LLMMessageIR | None = None,
                         metadata: Mapping[str, Any] | None = None) -> L1TurnIR:
        turn = self.store.begin(str(turn_id), user_text=user_text,
                                user_message=user_message, metadata=metadata)
        return turn

    def stream_right_assistant(self, turn_id: str, message: LLMMessageIR) -> None:
        self.store.stream_assistant(str(turn_id), message)

    def append_right_tool_result(self, token: ProducerToken, result: ToolResultIR) -> L1TurnIR:
        self._require_incarnation(token)
        turn = self.store.require_active(token.turn_id)
        updated = turn.append_tool_result(result)
        self.store.replace(updated)
        return updated

    def append_right_user(self, turn_id: str, message: LLMMessageIR) -> L1TurnIR:
        turn = self.store.require_active(str(turn_id))
        updated = turn.append(message)
        self.store.replace(updated)
        return updated

    def boundary_keep_prefix(self, turn_id: str) -> int:
        """Intra-cut message count ``turn_id`` must freeze at settlement (F2)."""

        if self._cut.intra_messages <= 0:
            return 0
        turns = self._ordered_turns()
        index = self._cut.turn_count
        if index < len(turns) and turns[index].turn_id == str(turn_id):
            return self._cut.intra_messages
        return 0

    def settle_right_turn(self, turn_id: str) -> L1TurnIR:
        turn = self.store.require_active(str(turn_id))
        updated = turn.settle(keep_prefix=self.boundary_keep_prefix(turn_id))
        self.store.replace(updated)
        return updated

    def interrupt_right_turn(self, turn_id: str, *, reason: str = "") -> L1TurnIR:
        turn = self.store.require_active(str(turn_id))
        updated = turn.interrupt(reason=reason)
        self.store.replace(updated)
        return updated

    def _require_incarnation(self, token: ProducerToken) -> None:
        if token.incarnation != self._incarnation:
            raise StaleProducer(
                "producer token belongs to an earlier session incarnation",
                token_incarnation=token.incarnation,
                current_incarnation=self._incarnation,
                turn_id=token.turn_id,
            )

    # ------------------------------------------------------------------
    # Promote — cut movement at request boundaries only (§3.3)
    # ------------------------------------------------------------------

    def promotable_turn_count(self, *, include_active: bool = False) -> tuple[int, int]:
        """Return (whole_turns, intra_messages) promotable under the cut rules."""

        turns = self._ordered_turns()
        run = self.active_run
        if run is not None:
            raise FrozenLeftDuringCompact(
                "the cut is frozen while a compact run is live",
                run_id=run.run_id,
                cut_id=self._cut.cut_id,
            )
        whole = self._cut.turn_count
        intra = self._cut.intra_messages
        count, moved_intra = whole, 0
        if intra:
            boundary = turns[whole] if whole < len(turns) else None
            if boundary is None:
                return whole, intra  # defensive: stale cut against a shrunken store
            if boundary.state == L1TurnState.ACTIVE:
                extended = self._closed_prefix_length(boundary)
                return (whole, extended) if extended > intra else (whole, intra)
            # Boundary turn closed since the last promote: it becomes wholly L.
            count = whole + 1
        index = count
        while index < len(turns):
            turn = turns[index]
            if turn.state == L1TurnState.ACTIVE:
                if not include_active:
                    break
                prefix_len = self._closed_prefix_length(turn)
                if prefix_len <= 0:
                    break
                moved_intra = prefix_len
                break
            count = index + 1
            index += 1
        return count, moved_intra

    @staticmethod
    def _closed_prefix_length(turn: L1TurnIR) -> int:
        """Length of the longest closed-group message prefix (I03).

        A closed group is protocol-complete AND answered: a trailing
        user-role message is the pending work tail awaiting its response —
        it stays on the right (PLAN §3.3: keep the newest work tail), never
        promotes into L just because no tool call is dangling.
        """

        messages = turn.messages
        limit = len(messages)
        for index, message in enumerate(messages):
            if message.state == MessageState.IN_PROGRESS:
                limit = index
                break
        best = 0
        for size in range(limit, 0, -1):
            calls, results = _protocol_ids(messages[:size])
            if calls == results:
                best = size
                break
        while best > 0 and messages[best - 1].role == MessageRole.USER:
            best -= 1
        return best

    def promote(
        self,
        *,
        include_active: bool = False,
        budget_guard: Callable[[CutPosition], bool] | None = None,
    ) -> CutPosition:
        """Advance the cut over closed history at a request boundary.

        ``budget_guard`` receives the *candidate* cut and may veto the move
        (L03: never promote the growth-driving tail just to compact it).
        """

        whole, intra = self.promotable_turn_count(include_active=include_active)
        if whole == self._cut.turn_count and intra == self._cut.intra_messages:
            return self._cut
        candidate = self._cut.advanced(turn_count=whole, intra_messages=intra)
        if budget_guard is not None and not budget_guard(candidate):
            return self._cut
        self._cut = candidate
        self._seal_frozen_boundary_prefix()
        return self._cut

    def _seal_frozen_boundary_prefix(self) -> None:
        """Closed-group sealing at the cut (review N1-R1).

        Assistant messages the cut freezes into L receive the existing
        neutral-reasoning retirement (``_close_message``) exactly once, at
        the moment the group closes — before any compact run can capture
        them.  After this the frozen prefix is representation-final:
        settlement and interrupt of the boundary turn reuse the same sealed
        objects (F2: L is never rewritten), and non-ACTIVE validation stays
        legal because no neutral reasoning remains under a closing turn.
        """

        if self._cut.intra_messages <= 0:
            return
        turns = self._ordered_turns()
        index = self._cut.turn_count
        if index >= len(turns):
            return
        boundary = turns[index]
        prefix = boundary.messages[: self._cut.intra_messages]
        sealed = tuple(_close_message(message) for message in prefix)
        if sealed == prefix:
            return
        updated = replace(
            boundary,
            messages=(
                *sealed,
                *boundary.messages[self._cut.intra_messages:],
            ),
            revision=boundary.revision + 1,
        )
        self.store.replace(updated)

    # ------------------------------------------------------------------
    # Compact runs — open, ready, terminal arbitration (§5)
    # ------------------------------------------------------------------

    def begin_compact(
        self,
        run_id: str,
        *,
        reason: str,
        parent_turn_id: str = "",
        deadline_at: float | None = None,
    ) -> LeftSnapshot:
        key = str(run_id or "").strip()
        if not key:
            raise HistoryRootError("run_id must be non-empty")
        if key in self._runs or (self._run is not None and self._run.run_id == key):
            raise HistoryRootError("run_id already used; tokens are never recycled", run_id=key)
        live = self.active_run
        if live is not None:
            raise CompactLaneBusy(
                "another compact run is live",
                active_run_id=live.run_id,
                requested_run_id=key,
            )
        left = self.left_turns()
        if not left:
            raise NoBeneficialCompaction("left is empty; nothing to compact", run_id=key)
        if all(self._is_summary_only(turn) for turn in left):
            raise NoBeneficialCompaction(
                "left is already the minimal summary seed", run_id=key,
                left_turn_count=len(left),
            )
        snapshot = LeftSnapshot(
            run_id=key,
            cut=self._cut,
            turns=tuple(left),
            stamp=self._left_stamp(),
        )
        self._run = CompactRun(
            run_id=key,
            reason=str(reason or "unspecified"),
            parent_turn_id=str(parent_turn_id or "").strip(),
            snapshot=snapshot,
            deadline_at=deadline_at,
        )
        return snapshot

    @staticmethod
    def _is_summary_only(turn: L1TurnIR) -> bool:
        return bool(turn.messages) and all(
            message.semantic_kind == "runtime_context_summary"
            for message in turn.messages
        )

    def _require_current_run(self, run_id: str) -> CompactRun:
        key = str(run_id or "")
        recorded = self._runs.get(key)
        if recorded is not None:
            raise TerminalClosed(
                f"run {key} is already terminal ({recorded.phase.value})",
                run_id=key,
                phase=recorded.phase.value,
                winner=recorded.winner,
            )
        run = self._run
        if run is None or run.run_id != key:
            raise StaleRun("not the current compact run", run_id=key,
                           current_run_id=run.run_id if run else "")
        return run

    def mark_ready(self, run_id: str, candidate: Any, *, candidate_id: str = "") -> None:
        run = self._require_current_run(run_id)
        if run.phase is CompactPhase.READY:
            raise CandidateConflict(
                "a candidate already holds this run's READY slot",
                run_id=run.run_id,
                accepted_candidate_id=run.candidate_id,
                rejected_candidate_id=str(candidate_id or ""),
            )
        if run.phase is not CompactPhase.RUNNING:
            raise TerminalClosed(
                f"run {run.run_id} is already terminal ({run.phase.value})",
                run_id=run.run_id,
                phase=run.phase.value,
                winner=run.winner,
            )
        run.phase = CompactPhase.READY
        run.candidate = candidate
        run.candidate_id = str(candidate_id or "")

    def _retire_run(self, run: CompactRun) -> None:
        """Store a terminal record without pinning retired content (F5).

        The archive keeps identity, cut/stamp, candidate id, and committed
        revision — enough for replayed outcomes and duplicate-delivery
        arbitration — and releases the captured left turns and the candidate
        object so compacted history can actually be collected.
        """

        run.snapshot = LeftSnapshot(
            run_id=run.snapshot.run_id,
            cut=run.snapshot.cut,
            turns=(),
            stamp=run.snapshot.stamp,
        )
        run.candidate = None
        self._runs[run.run_id] = run

    def close_run(
        self, run_id: str, *, cancelled: bool, reason: str = ""
    ) -> CompactOutcome | None:
        """Idempotent terminal close for one operation handle (review R2-b).

        Acts only while ``run_id`` is still the live current run.  A run
        that already reached a terminal state returns its recorded outcome;
        a run this root no longer knows (retired by reset, or superseded by
        a newer run) returns None — a vanished run is terminal by
        construction, and a late cleanup must not raise StaleRun out of its
        own finally path or disturb whatever run now owns the lane.
        """

        key = str(run_id or "")
        recorded = self._runs.get(key)
        if recorded is not None:
            left_revision = (
                int(recorded.committed_left_revision)
                if recorded.committed_left_revision is not None
                else self._cut.revision
            )
            return CompactOutcome(
                run_id=key,
                status=recorded.phase.value,
                left_revision=left_revision,
                replayed=True,
                detail=recorded.terminal_detail,
            )
        run = self._run
        if run is None or run.run_id != key:
            return None
        if run.terminal:
            return CompactOutcome(
                run_id=key,
                status=run.phase.value,
                left_revision=self._cut.revision,
                replayed=True,
                detail=run.terminal_detail,
            )
        if cancelled:
            return self.cancel(key, reason=reason or "cancelled")
        return self.fail(key, reason=reason or "failed")

    def cancel(self, run_id: str, *, reason: str = "") -> CompactOutcome:
        key = str(run_id or "")
        recorded = self._runs.get(key)
        if recorded is not None:
            return CompactOutcome(
                run_id=key,
                status=recorded.phase.value,
                left_revision=self._cut.revision,
                replayed=True,
                detail=recorded.terminal_detail,
            )
        run = self._require_current_run(key)
        if run.phase.terminal:  # pragma: no cover - defensive
            raise TerminalClosed("run already terminal", run_id=key,
                                 phase=run.phase.value, winner=run.winner)
        run.phase = CompactPhase.CANCELLED
        run.winner = "cancelled"
        run.terminal_detail = str(reason or "cancelled")
        self._retire_run(run)
        return CompactOutcome(
            run_id=key,
            status="cancelled",
            left_revision=self._cut.revision,
            detail=run.terminal_detail,
        )

    def fail(self, run_id: str, *, reason: str = "") -> CompactOutcome:
        key = str(run_id or "")
        recorded = self._runs.get(key)
        if recorded is not None:
            return CompactOutcome(
                run_id=key,
                status=recorded.phase.value,
                left_revision=self._cut.revision,
                replayed=True,
                detail=recorded.terminal_detail,
            )
        run = self._require_current_run(key)
        run.phase = CompactPhase.FAILED
        run.winner = "failed"
        run.terminal_detail = str(reason or "failed")
        self._retire_run(run)
        return CompactOutcome(
            run_id=key,
            status="failed",
            left_revision=self._cut.revision,
            detail=run.terminal_detail,
        )

    def expire_deadline(self, now: float) -> CompactOutcome | None:
        """Fail the live run if its absolute deadline passed (F17, X08)."""

        run = self.active_run
        if run is None or run.deadline_at is None or now < run.deadline_at:
            return None
        return self.fail(run.run_id, reason="deadline")

    def commit(
        self,
        run_id: str,
        *,
        build_summary_turn: Callable[[Any], L1TurnIR] = default_summary_builder,
    ) -> CompactOutcome:
        key = str(run_id or "")
        recorded = self._runs.get(key)
        if recorded is not None:
            if recorded.phase is CompactPhase.COMMITTED:
                return CompactOutcome(
                    run_id=key,
                    status="committed",
                    left_revision=int(recorded.committed_left_revision or 0),
                    seed_turn_id=SUMMARY_TURN_ID,
                    replayed=True,
                )
            raise TerminalClosed(
                f"run {key} ended as {recorded.phase.value}; a late completion "
                "cannot install",
                run_id=key,
                phase=recorded.phase.value,
                winner=recorded.winner,
            )
        run = self._require_current_run(key)
        if run.phase is not CompactPhase.READY:
            raise HistoryRootError(
                "commit requires a validated READY candidate",
                run_id=key,
                phase=run.phase.value,
            )
        if run.deadline_at is not None and time.monotonic() >= float(run.deadline_at):
            # F1/N03: an expired run loses commit eligibility; the terminal
            # record is the sweep, and no second claimant is needed.  `>=`
            # matches expire_deadline's expiry semantics.
            self.fail(key, reason="deadline")
            raise HistoryRootError(
                "compact run deadline expired before commit",
                run_id=key,
                phase="failed",
            )
        # ---- local construction first; anything below may throw without
        # touching the root (I08 / C05).
        seed_turn = build_summary_turn(run.candidate)
        if not isinstance(seed_turn, L1TurnIR) or not seed_turn.messages:
            raise HistoryRootError("summary builder must return a non-empty L1TurnIR",
                                   run_id=key)
        if self._left_stamp() != run.snapshot.stamp:
            raise LeftChangedSinceCapture(
                "left changed since capture",
                run_id=key,
                expected_stamp=run.snapshot.stamp,
                actual_stamp=self._left_stamp(),
            )
        right = self._physical_right_for_install()
        # ---- final admission, immediately before the non-awaiting publish
        # (review N1-R3): all expensive local construction is complete; the
        # run must still be ours and READY, the captured left identity must
        # still match, and the absolute deadline must not have passed.
        # A deadline crossed after a linearized success never rolls it back.
        current = self._require_current_run(key)
        if current is not run or current.phase is not CompactPhase.READY:
            raise HistoryRootError(
                "run lost commit eligibility during construction",
                run_id=key,
                phase=current.phase.value,
            )
        if self._left_stamp() != run.snapshot.stamp:
            raise LeftChangedSinceCapture(
                "left changed since capture",
                run_id=key,
                expected_stamp=run.snapshot.stamp,
                actual_stamp=self._left_stamp(),
            )
        if run.deadline_at is not None and time.monotonic() >= float(run.deadline_at):
            self.fail(key, reason="deadline")
            raise HistoryRootError(
                "compact run deadline expired before publish",
                run_id=key,
                phase="failed",
            )
        # ---- single non-awaiting publish section (I08).
        self.store.replace_all([seed_turn, *right])
        self._cut = CutPosition(
            turn_count=1,
            intra_messages=0,
            revision=self._cut.revision + 1,
            cut_id=f"cut-{uuid4().hex[:12]}",
        )
        run.phase = CompactPhase.COMMITTED
        run.winner = "committed"
        run.committed_left_revision = self._cut.revision
        self._left_generation += 1
        self._retire_run(run)
        return CompactOutcome(
            run_id=key,
            status="committed",
            left_revision=self._cut.revision,
            seed_turn_id=SUMMARY_TURN_ID,
        )

    def _physical_right_for_install(self) -> tuple[L1TurnIR, ...]:
        """Materialize the install-time right side (I05: R content preserved).

        A boundary turn whose closed prefix moved into L keeps its identity
        as a revision-advanced record holding exactly its suffix (or an
        empty ACTIVE remainder when everything was promoted) so later
        rounds of the same logical turn keep a home (I15).

        A turn with a live streaming round contributes its round-aware
        current content (streamed bytes are never dropped); the round
        re-anchors onto the fresh record with its next fragment.  This is
        what lets commit coexist with still-pending authorized tool groups
        (F04) instead of refusing them.
        """

        turns = self._ordered_turns()
        folded: set[str] = set()
        if not self._cut.intra_messages:
            tail = list(turns[self._cut.turn_count:])
        else:
            boundary = turns[self._cut.turn_count]
            current = self.store.get(boundary.turn_id) or boundary
            remainder = replace(
                current,
                messages=current.messages[self._cut.intra_messages:],
                revision=current.revision + 1,
            )
            tail = [remainder, *turns[self._cut.turn_count + 1:]]
            folded.add(boundary.turn_id)
        physical: list[L1TurnIR] = []
        for turn in tail:
            if turn.turn_id not in folded and self.store.has_open_round(turn.turn_id):
                snapshot = self.store.get(turn.turn_id) or turn
                turn = replace(snapshot, revision=snapshot.revision + 1)
            physical.append(turn)
        return tuple(physical)

    # ------------------------------------------------------------------
    # Interrupt arbitration (§5.1)
    # ------------------------------------------------------------------

    def interrupt_compaction_for_turn(self, turn_id: str) -> str:
        """Arbitrate /interrupt against the live compact run.

        Returns ``"cancelled"`` when the run belonged to this turn and had
        not committed, ``"committed"`` when install already won, and
        ``"no_active_turn"`` otherwise (idle manual compaction is not a
        turn and is never fake-cancelled — X03).
        """

        run = self.active_run or self._run
        if run is None:
            return "no_active_turn"
        if run.parent_turn_id and run.parent_turn_id == str(turn_id):
            if run.phase is CompactPhase.COMMITTED:
                return "committed"
            self.cancel(run.run_id, reason="interrupt")
            return "cancelled"
        return "no_active_turn"

    # ------------------------------------------------------------------
    # Reset (§5.3)
    # ------------------------------------------------------------------

    def reset(self) -> str:
        """Invalidate the live run, clear history, and roll the incarnation."""

        run = self.active_run
        if run is not None:
            self.cancel(run.run_id, reason="reset")
        self.store.clear()
        self._runs.clear()
        self._left_generation = 0
        self._incarnation = f"inc-{uuid4().hex[:12]}"
        self._cut = CutPosition(turn_count=0, intra_messages=0, revision=0,
                                cut_id=f"cut-{uuid4().hex[:12]}")
        self._run = None
        return self._incarnation
