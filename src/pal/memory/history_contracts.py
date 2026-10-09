"""History ownership data and read/transaction ports, independent of storage."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, TYPE_CHECKING
from uuid import uuid4

from pal.llm.ir import LLMMessageIR
from pal.shared.tool_protocol import ToolResultIR

if TYPE_CHECKING:
    from pal.memory.turn_ir import L1TurnIR
    from pal.memory.continuity import Continuity


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


@dataclass(frozen=True)
class PreparedHistorySubmission:
    incarnation: str
    cut: CutPosition
    target: CutPosition
    messages: tuple[LLMMessageIR, ...]
    required_ids: frozenset[str]


class HistoryReadPort(Protocol):
    @property
    def turns(self) -> tuple[L1TurnIR, ...]: ...
    @property
    def continuity(self) -> Continuity | None: ...
    @property
    def summary_turn_id(self) -> str: ...
    def get(self, turn_id: str) -> L1TurnIR | None: ...
    def has_open_round(self, turn_id: str) -> bool: ...
    def add_change_listener(
        self, listener: Callable[[tuple[L1TurnIR, ...], tuple[L1TurnIR, ...]], None], *,
        validate: Callable[[tuple[L1TurnIR, ...]], None] | None = None,
    ) -> None:
        """Core ownership hooks only: validation may reject before publish;
        listener must handle ordinary failures without raising. Extension
        notifications use the post-root-commit service hook or Core event bus.
        Process-control BaseExceptions are outside the no-throw contract.
        """
        ...
    def remove_change_listener(
        self, listener: Callable[[tuple[L1TurnIR, ...], tuple[L1TurnIR, ...]], None],
    ) -> None: ...


class HistoryRootPort(Protocol):

    @property
    def incarnation(self) -> str:
        ...

    @property
    def cut(self) -> CutPosition:
        ...

    @property
    def left_revision(self) -> int:
        ...

    @property
    def left_generation(self) -> int:
        ...

    @property
    def active_run(self) -> CompactRun | None:
        ...

    @property
    def last_run(self) -> CompactRun | None:
        ...

    def run_record(self, run_id: str) -> CompactRun | None:
        ...

    def all_turns(self) -> tuple[L1TurnIR, ...]:
        ...

    def left_turns(self) -> tuple[L1TurnIR, ...]:
        ...

    def right_turns(self) -> tuple[L1TurnIR, ...]:
        ...

    def left_messages(self) -> tuple[LLMMessageIR, ...]:
        ...

    def right_messages(self) -> tuple[LLMMessageIR, ...]:
        ...

    def owner_state(self) -> dict[str, Any]:
        ...

    def restore_owner_state(self, *, incarnation: str, cut: CutPosition, left_generation: int) -> None:
        ...

    def producer_token(self, turn_id: str) -> ProducerToken:
        ...

    def begin_right_turn(self, turn_id: str, *, user_text: str='', user_message: LLMMessageIR | None=None, metadata: Mapping[str, Any] | None=None) -> L1TurnIR:
        ...

    def stream_right_assistant(self, turn_id: str, message: LLMMessageIR) -> None:
        ...

    def append_right_tool_result(self, token: ProducerToken, result: ToolResultIR) -> L1TurnIR:
        ...

    def append_right_user(self, turn_id: str, message: LLMMessageIR) -> L1TurnIR:
        ...

    def boundary_keep_prefix(self, turn_id: str) -> int:
        ...

    def settle_right_turn(self, turn_id: str) -> L1TurnIR:
        ...

    def interrupt_right_turn(self, turn_id: str, *, reason: str='') -> L1TurnIR:
        ...

    def prepare_submission(self, message_ids: Iterable[str]) -> PreparedHistorySubmission:
        ...

    def commit_submission(self, prepared: PreparedHistorySubmission, message_ids: Iterable[str]) -> bool:
        ...

    def promotable_turn_count(self, *, include_active: bool=False) -> tuple[int, int]:
        ...

    def promote(self, *, include_active: bool=False, budget_guard: Callable[[CutPosition], bool] | None=None) -> CutPosition:
        ...

    def begin_compact(self, run_id: str, *, reason: str, parent_turn_id: str='', deadline_at: float | None=None) -> LeftSnapshot:
        ...

    def mark_ready(self, run_id: str, candidate: Any, *, candidate_id: str='') -> None:
        ...

    def close_run(self, run_id: str, *, cancelled: bool, reason: str='') -> CompactOutcome | None:
        ...

    def cancel(self, run_id: str, *, reason: str='') -> CompactOutcome:
        ...

    def fail(self, run_id: str, *, reason: str='') -> CompactOutcome:
        ...

    def expire_deadline(self, now: float) -> CompactOutcome | None:
        ...

    def commit(self, run_id: str, *, build_summary_turn: Callable[[Any], L1TurnIR]=...) -> CompactOutcome:
        ...

    def interrupt_compaction_for_turn(self, turn_id: str) -> str:
        ...

    def reset(self) -> str:
        ...
