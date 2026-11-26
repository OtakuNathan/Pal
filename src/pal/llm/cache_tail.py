"""Pure chronological selection; cache markers are opportunities, not receipts."""
from collections.abc import Iterable

POLICY = "openai_session_chase_tail_v2"
MarkerPath = tuple[str | int, ...]


def choose_paths(stable: MarkerPath | None, anchor: MarkerPath | None,
                 candidates: Iterable[MarkerPath], limit: int) -> tuple[MarkerPath, ...]:
    fixed = tuple(dict.fromkeys(p for p in (stable, anchor) if p is not None))
    recent = sorted(set(candidates) - set(fixed))
    remaining = max(0, limit - len(fixed))
    return tuple(sorted((*fixed, *(recent[-remaining:] if remaining else ()))))
