"""Bounded history of submitted positions; provider responses cannot move it."""
from dataclasses import dataclass

from pal.llm.cache_wire import Boundary

POLICY = "openai_eager_tail_v1"


@dataclass
class TailHistory:
    turn: str = ""
    epoch: tuple = ()
    generation: int = 0
    sequence: int = 0
    tails: tuple[Boundary, ...] = ()
    continuity_id: str = ""
    continuity_turn: str = ""
    last_access: float = 0
    closed: bool = False

    def refresh(self, turn: str, epoch: tuple, valid: dict, now: float) -> None:
        if turn != self.turn or epoch != self.epoch:
            self.turn, self.epoch = turn, epoch
            self.generation += 1
            self.tails = ()
            self.closed = False
        retained = tuple(b for b in self.tails if valid.get(b.path) == b)
        if retained != self.tails:
            self.generation += 1
        self.tails = retained
        self.last_access = now

    def previous(self, current: Boundary | None) -> Boundary | None:
        if current is None or self.closed:
            return None
        return next((b for b in reversed(self.tails) if b.path < current.path), None)

    def submit(self, current: Boundary | None, generation: int, sequence: int) -> None:
        # Sequence is reserved by planning and consumed once at submission.
        # Same-tail retries do not evict the previous distinct position.
        if self.closed or generation != self.generation or sequence <= self.sequence:
            return
        self.sequence = sequence
        if current is not None and current not in self.tails:
            if not self.tails or current.path > self.tails[-1].path:
                self.tails = (*self.tails, current)[-2:]

    def end_turn(self, turn: str) -> None:
        if turn == self.turn:
            self.tails = ()
            self.closed = True
            self.generation += 1

    def snapshot(self) -> dict:
        return {"turn_id": self.turn, "generation": self.generation,
                "submitted_sequence": self.sequence, "closed": self.closed,
                "tails": [{"message_id": b.message_id, "path": b.path,
                           "fingerprint": b.fingerprint} for b in self.tails]}
