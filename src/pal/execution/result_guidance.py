"""Helpers for result-side guidance: failure pick-best, action identity,
and single-result dedup (task package v2 §6-§8).

These functions neither execute tools, capture snapshots, nor consult any
display history. Malformed candidates produce local diagnostics. Selection
depends only on the captured call context and the current result's facts.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from pal.shared.tool_protocol import ToolAffordance

# §8.4: a small internal constant cap; no new user configuration matrix.
MAX_RESULT_AFFORDANCES = 3

_CALL_TOOL = "call_tool"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class ActionKey:
    """Semantic identity of one suggested action (§8.2)."""

    alias: str
    arguments_json: str


def _canonical_json(value: Any) -> str:
    """Sort object keys, preserve array order and value-type distinctions."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unwrap_call_tool(arguments: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """Unwrap only the known call_tool envelope; never rewrite other shapes."""

    if not isinstance(arguments, Mapping):
        return None
    name = arguments.get("name")
    inner = arguments.get("args", {})
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(inner, Mapping):
        return None
    if set(arguments.keys()) - {"name", "args"}:
        return None
    return name, inner


def action_key(affordance: ToolAffordance) -> ActionKey:
    """Direct ``foo(args)`` and ``call_tool(name='foo', args=args)`` share a key.

    ``read_tool(name='foo')`` remains a different action from executing foo.
    """

    if str(affordance.tool or "") == _CALL_TOOL:
        unwrapped = _unwrap_call_tool(affordance.arguments)
        if unwrapped is not None:
            alias, arguments = unwrapped
            return ActionKey(alias=str(alias), arguments_json=_canonical_json(arguments))
    return ActionKey(
        alias=str(affordance.tool or ""),
        arguments_json=_canonical_json(affordance.arguments),
    )


def normalize_affordances(
    candidates: Iterable[ToolAffordance],
    *,
    limit: int | None = MAX_RESULT_AFFORDANCES,
) -> list[ToolAffordance]:
    """Dedup by action key within one logical result, keeping producer order.

    Choice precedes dedup (§8.1): producers must not emit unwanted candidates
    in the first place; this pass only merges genuine repeats. Idempotent.
    """

    merged: dict[ActionKey, ToolAffordance] = {}
    for candidate in candidates:
        try:
            candidate = ToolAffordance.model_validate(candidate)
            key = action_key(candidate)
        except Exception:
            _LOGGER.warning("Dropping malformed tool result affordance", exc_info=True)
            continue
        if key not in merged:
            merged[key] = candidate
    values = list(merged.values())
    return values if limit is None else values[: max(0, limit)]


def filter_resolvable_affordances(
    candidates: Sequence[ToolAffordance],
    *,
    resolvable_aliases: Iterable[str],
) -> list[ToolAffordance]:
    """Drop candidates whose action alias is not usable in the captured view.

    Validation happens against the generation and role scope captured for the
    logical call (§8.3); suggestions never widen capabilities. Unusable
    candidates are dropped, not rewritten into discovery actions.
    """

    known = set(resolvable_aliases)
    return [item for item in candidates if action_key(item).alias in known]


def resolve_failure_guidance(
    *,
    handler_recovery_hint: str,
    handler_affordances: Sequence[ToolAffordance],
    declared_failure_next_steps: str,
) -> tuple[str, list[ToolAffordance]]:
    """Pick the best failure guidance instead of stacking every source (§6.1).

    Handler-provided guidance wins; the tool's declared fallback fills the
    recovery hint only when the handler offered nothing more specific. No
    affordance is ever fabricated here.
    """

    hint = str(handler_recovery_hint or "").strip()
    # Apply the count limit only after route/schema/scope filtering.
    actions = normalize_affordances(handler_affordances, limit=None)
    if not hint and not actions:
        hint = str(declared_failure_next_steps or "").strip()
    return hint, actions


__all__ = [
    "MAX_RESULT_AFFORDANCES",
    "ActionKey",
    "action_key",
    "filter_resolvable_affordances",
    "normalize_affordances",
    "resolve_failure_guidance",
]
