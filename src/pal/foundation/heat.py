from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


DEFAULT_HOT_TTL = 5
DEFAULT_GHOST_TTL = 3
MAX_RENEWAL_COUNT = 3


class HeatLevel(Enum):
    DORMANT = "DORMANT"
    HOT = "HOT"
    GHOST = "GHOST"


@dataclass(frozen=True)
class HeatPolicy:
    hot_ttl: int = DEFAULT_HOT_TTL
    ghost_ttl: int = DEFAULT_GHOST_TTL
    max_renewal_count: int = MAX_RENEWAL_COUNT


@dataclass(frozen=True)
class HeatState:
    key: str
    heat_level: HeatLevel = HeatLevel.DORMANT
    hot_ttl: int = 0
    ghost_ttl: int = 0
    renewal_count: int = 0

    @property
    def entry_id(self) -> str:
        return self.key

    @property
    def affordance_id(self) -> str:
        return self.key


@dataclass(frozen=True)
class HeatTransition:
    key: str
    event: str
    state: HeatState | None
    expired: bool = False


@dataclass(frozen=True)
class HeatStateMachine:
    policy: HeatPolicy = field(default_factory=HeatPolicy)

    def promote_to_hot(self, key: str, current: HeatState | None = None) -> HeatTransition:
        normalized_key = _normalize_key(key)
        if current is None or current.heat_level == HeatLevel.DORMANT:
            return HeatTransition(
                key=normalized_key,
                event="hot_promoted",
                state=HeatState(
                    key=normalized_key,
                    heat_level=HeatLevel.HOT,
                    hot_ttl=self.policy.hot_ttl,
                    ghost_ttl=0,
                    renewal_count=0,
                ),
            )
        if current.heat_level == HeatLevel.HOT:
            return HeatTransition(
                key=normalized_key,
                event="hot_refreshed",
                state=HeatState(
                    key=normalized_key,
                    heat_level=HeatLevel.HOT,
                    hot_ttl=self.policy.hot_ttl,
                    ghost_ttl=0,
                    renewal_count=current.renewal_count,
                ),
            )
        if current.renewal_count >= self.policy.max_renewal_count:
            return HeatTransition(key=normalized_key, event="ghost_force_dormant", state=current)
        renewal_count = current.renewal_count + 1
        return HeatTransition(
            key=normalized_key,
            event="ghost_reactivated",
            state=HeatState(
                key=normalized_key,
                heat_level=HeatLevel.HOT,
                hot_ttl=self.policy.hot_ttl,
                ghost_ttl=0,
                renewal_count=renewal_count,
            ),
        )

    def tick(self, current: HeatState) -> HeatTransition:
        normalized_key = _normalize_key(current.key)
        if current.heat_level == HeatLevel.HOT:
            hot_ttl = current.hot_ttl - 1
            if hot_ttl <= 0:
                if current.renewal_count >= self.policy.max_renewal_count:
                    return HeatTransition(key=normalized_key, event="ghost_force_dormant", state=None, expired=True)
                return HeatTransition(
                    key=normalized_key,
                    event="hot_to_ghost",
                    state=HeatState(
                        key=normalized_key,
                        heat_level=HeatLevel.GHOST,
                        hot_ttl=0,
                        ghost_ttl=self.policy.ghost_ttl,
                        renewal_count=current.renewal_count,
                    ),
                )
            return HeatTransition(
                key=normalized_key,
                event="hot_tick",
                state=HeatState(
                    key=normalized_key,
                    heat_level=HeatLevel.HOT,
                    hot_ttl=hot_ttl,
                    ghost_ttl=0,
                    renewal_count=current.renewal_count,
                ),
            )
        if current.heat_level == HeatLevel.GHOST:
            ghost_ttl = current.ghost_ttl - 1
            if ghost_ttl <= 0:
                return HeatTransition(key=normalized_key, event="ghost_to_dormant", state=None, expired=True)
            return HeatTransition(
                key=normalized_key,
                event="ghost_tick",
                state=HeatState(
                    key=normalized_key,
                    heat_level=HeatLevel.GHOST,
                    hot_ttl=0,
                    ghost_ttl=ghost_ttl,
                    renewal_count=current.renewal_count,
                ),
            )
        return HeatTransition(key=normalized_key, event="dormant", state=None, expired=True)


@dataclass
class HeatStateRegistry:
    machine: HeatStateMachine = field(default_factory=HeatStateMachine)
    states: dict[str, HeatState] = field(default_factory=dict)

    def promote_to_hot(self, key: str) -> HeatTransition:
        normalized_key = _normalize_key(key)
        transition = self.machine.promote_to_hot(normalized_key, self.states.get(normalized_key))
        self._apply(transition)
        return transition

    def tick(self) -> list[HeatTransition]:
        transitions: list[HeatTransition] = []
        for key in list(self.states):
            transition = self.machine.tick(self.states[key])
            self._apply(transition)
            transitions.append(transition)
        return transitions

    def get(self, key: str) -> HeatState | None:
        return self.states.get(_normalize_key(key))

    def remove(self, key: str) -> None:
        self.states.pop(_normalize_key(key), None)

    def clear(self) -> None:
        self.states.clear()

    def hot_keys(self) -> tuple[str, ...]:
        return tuple(key for key, state in self.states.items() if state.heat_level == HeatLevel.HOT)

    def _apply(self, transition: HeatTransition) -> None:
        if transition.state is None:
            self.states.pop(transition.key, None)
        else:
            self.states[transition.key] = transition.state


def _normalize_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not normalized:
        raise ValueError("heat state key must not be empty")
    return normalized
