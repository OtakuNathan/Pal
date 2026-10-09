"""Finite admission/lifetime model, independent of the runtime implementation.

Composition: a blocking acquisition owns a lease until the async waiter either
enters the body or drains cancellation and releases it.
"""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class State:
    phase: str = "queued"
    writer_held: bool = True
    lease: bool = False
    cancellations: int = 0
    dispatched: bool = False


def successors(s: State, *, broken_cancel=False):
    if s.writer_held:
        yield "release lifecycle writer", replace(s, writer_held=False)
    if s.phase in {"queued", "draining", "acquired"} and s.cancellations < 2:
        phase = "abandoned" if broken_cancel and s.phase == "draining" else "draining"
        yield "cancel waiter", replace(s, phase=phase, cancellations=s.cancellations + 1)
    if not s.writer_held and not s.lease and s.phase in {"queued", "draining", "abandoned"}:
        yield "worker acquires", replace(s, lease=True, phase="acquired" if s.phase == "queued" else s.phase)
    if s.phase == "draining" and s.lease:
        yield "drain and release", replace(s, phase="done", lease=False)
    if s.phase == "acquired":
        yield "dispatch", replace(s, phase="running", dispatched=True)
    if s.phase == "running":
        yield "finish and release", replace(s, phase="done", lease=False)


def violations(s: State):
    return (
        s.phase in {"done", "abandoned"} and s.lease,
        s.dispatched and s.cancellations > 0,
        s.phase == "running" and not s.lease,
    )


def explore(**options):
    seen = {State()}
    pending = [(State(), ())]
    while pending:
        state, trace = pending.pop()
        if any(violations(state)):
            return seen, trace
        for action, successor in successors(state, **options):
            if successor not in seen:
                seen.add(successor)
                pending.append((successor, trace + (action,)))
    return seen, None
