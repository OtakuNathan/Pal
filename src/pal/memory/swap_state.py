"""Executable migration decisions, exported verbatim as a finite TLA relation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SwapPhase(StrEnum):
    PREPARING = "preparing"
    COPYING = "copying"
    CATCHING_UP = "catching_up"
    READY = "ready"
    COMMITTED = "committed"
    INDEXING = "indexing"
    COMPLETE = "complete"
    SUPERSEDED = "superseded"


class SwapEvent(StrEnum):
    SNAPSHOT = "snapshot"
    COPIED = "copied"
    VERIFIED = "verified"
    COMMIT = "commit"
    RETRY = "retry"
    RECOVER = "recover"
    INDEX = "index"
    INDEXED = "indexed"
    SUPERSEDE = "supersede"
    ABANDON = "abandon"


class SwapLaunch(StrEnum):
    IGNORE = "ignore"
    SUPERSEDE = "supersede"
    RUN = "run"


@dataclass(frozen=True)
class SwapFacts:
    source_current: bool = False
    caught_up: bool = False
    fenced: bool = False
    published: bool = False
    indexed: bool = False


PRECOMMIT_PHASES = frozenset((SwapPhase.PREPARING, SwapPhase.COPYING, SwapPhase.CATCHING_UP, SwapPhase.READY))


def can_sleep(notice_required: bool, notice_delivered: bool) -> bool:
    return not notice_required or notice_delivered


def can_wake(published: bool, bound: bool, source_released: bool) -> bool:
    return source_released and (not published or bound)


def binding_ready(repository_current: bool, backend_ready: bool, selected: bool) -> bool:
    return repository_current and backend_ready and selected


def decide_launch(phase: SwapPhase, facts: SwapFacts, *, paused: bool) -> SwapLaunch:
    if phase in (SwapPhase.COMPLETE, SwapPhase.SUPERSEDED):
        return SwapLaunch.IGNORE
    if not facts.published and (phase in (SwapPhase.COMMITTED, SwapPhase.INDEXING)
                                or not facts.source_current):
        return SwapLaunch.SUPERSEDE
    return SwapLaunch.IGNORE if paused else SwapLaunch.RUN


def can_publish(phase: SwapPhase, facts: SwapFacts) -> bool:
    return (phase is SwapPhase.READY and facts.source_current and facts.caught_up
            and facts.fenced and not facts.published)


def reduce_swap(phase: SwapPhase, event: SwapEvent, facts: SwapFacts) -> SwapPhase:
    if event is SwapEvent.ABANDON and phase in PRECOMMIT_PHASES and not facts.published:
        return SwapPhase.SUPERSEDED
    if event is SwapEvent.SNAPSHOT and phase is SwapPhase.PREPARING and facts.source_current and not facts.published:
        return SwapPhase.COPYING
    if event is SwapEvent.COPIED and phase is SwapPhase.COPYING and not facts.published:
        return SwapPhase.CATCHING_UP
    if (event is SwapEvent.VERIFIED and phase is SwapPhase.CATCHING_UP and facts.source_current
            and facts.caught_up and facts.fenced and not facts.published):
        return SwapPhase.READY
    if event is SwapEvent.COMMIT and phase is SwapPhase.READY and facts.published and facts.fenced and facts.caught_up:
        return SwapPhase.COMMITTED
    if event is SwapEvent.RETRY and phase in PRECOMMIT_PHASES and facts.source_current and not facts.published:
        return SwapPhase.PREPARING
    if event is SwapEvent.RECOVER and phase in PRECOMMIT_PHASES and facts.published:
        return SwapPhase.COMMITTED
    if event is SwapEvent.INDEX and phase is SwapPhase.COMMITTED and facts.published:
        return SwapPhase.INDEXING
    if event is SwapEvent.INDEXED and phase is SwapPhase.INDEXING and facts.published and facts.indexed:
        return SwapPhase.COMPLETE
    if event is SwapEvent.SUPERSEDE and phase in (SwapPhase.COMMITTED, SwapPhase.INDEXING) and not facts.published:
        return SwapPhase.SUPERSEDED
    if event is SwapEvent.SUPERSEDE and phase in PRECOMMIT_PHASES and not facts.source_current and not facts.published:
        return SwapPhase.SUPERSEDED
    raise ValueError(f"Illegal memory swap transition: {phase}, {event}, {facts}")
