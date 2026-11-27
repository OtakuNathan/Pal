"""Read immutable effort pins from the same L1 history used for requests."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

from pal.llm.compaction import NATIVE_WINDOW_KEY, is_native_window
from pal.llm.effort_updates import EFFORT_PIN, EFFORT_UPDATE, binding, binding_matches, can_append
from pal.shared.json_values import thaw_json

if TYPE_CHECKING:
    from pal.llm.ir import LLMMessageIR
    from pal.memory.contracts import MemoryServicePort
    from pal.memory.turn_ir import L1TurnIR


def _epoch(memory: MemoryServicePort) -> list[Any]:
    root = memory.history_root
    return [root.incarnation, root.left_generation]


def _matching(value: Any, facts: Mapping[str, Any]) -> bool:
    return binding_matches(value, facts)


def _native_pin(message: LLMMessageIR, facts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if message.replay is not None and is_native_window(message):
        pin = message.replay.payload[NATIVE_WINDOW_KEY].get(EFFORT_PIN)
        if _matching(pin, facts):
            return pin
    return None


def history_effort_pin(memory: MemoryServicePort, facts: Mapping[str, Any]) -> dict[str, Any] | None:
    for turn in reversed(memory.history.turns):
        pin = turn.metadata.get(EFFORT_PIN)
        if _matching(pin, facts):
            return thaw_json(pin)
        for message in reversed(turn.messages):
            pin = _native_pin(message, facts)
            if pin is not None:
                # A native seed belongs to a newly installed generation but
                # carries the settings of its source rather than a new turn.
                return {**thaw_json(pin), "epoch": []}
    return None


def _has_retained_update(memory: MemoryServicePort, facts: Mapping[str, Any], *, native: bool) -> bool:
    for turn in memory.history.turns:
        for message in turn.messages:
            if _matching(message.metadata.get(EFFORT_UPDATE), facts):
                return True
            if native:
                pin = _native_pin(message, facts)
                if pin is not None and pin["base"] != pin["effective"]:
                    return True
    return False


def _for_visible_prefix(memory: MemoryServicePort, facts: Mapping[str, Any], pin: Mapping[str, Any]) -> dict[str, Any]:
    result = thaw_json(pin)
    if (tuple(pin.get("epoch", ())) != tuple(_epoch(memory))
            and facts.get("supports_effort_updates")
            and not _has_retained_update(memory, facts, native=True)):
        # Semantic compaction may remove the event while a turn's R survives.
        # On its new prefix, preserve the effective setting as the root.
        result["base"] = pin["effective"]
    return result


def plan_effort_pin(memory: MemoryServicePort, facts: Mapping[str, Any], desired: str
                    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    route = binding(facts)
    epoch = _epoch(memory)
    previous = history_effort_pin(memory, facts)
    if previous is not None and tuple(previous.get("epoch", ())) != tuple(epoch):
        previous = _for_visible_prefix(memory, facts, previous)
        # A new prefix admits pending root-only changes. Native seed effort
        # restoration is unnecessary when starting a different root. A real
        # retained user update, however, must never be rewritten or ignored.
        if (not facts.get("supports_effort_updates")
                or (not can_append(facts, previous["base"], desired)
                    and not _has_retained_update(memory, facts, native=False))):
            previous = None
    if previous is not None and previous["base"] not in facts.get("thinking_levels", ()):
        previous = None  # The endpoint configuration no longer allows that root.
    base = previous["base"] if previous else desired
    effective = previous["effective"] if previous else desired
    update = None
    if desired != effective and can_append(facts, base, desired):
        effective = desired
        update = {**route, "effort": desired}
    return {**route, "epoch": epoch, "base": base, "effective": effective}, update


def pinned_think_levels(memory: MemoryServicePort, facts: Mapping[str, Any], levels: Mapping[str, Any],
                        *, active_turn: L1TurnIR | None = None) -> dict[str, Any]:
    result = dict(levels)
    pin = active_turn.metadata.get(EFFORT_PIN) if active_turn is not None else None
    if not _matching(pin, facts):
        pin = history_effort_pin(memory, facts)
    if pin and pin["base"] in facts.get("thinking_levels", ()):
        pin = _for_visible_prefix(memory, facts, pin)
        result[facts["endpoint_id"]] = pin["base"]
    return result
