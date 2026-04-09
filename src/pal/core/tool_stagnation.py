from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pal.shared import GuardAction, GuardStatus


NOISY_RESULT_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "timestamp",
        "trace_id",
        "request_id",
        "event_id",
    }
)


def _stable_serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _normalize_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_result(item)
            for key, item in sorted(value.items())
            if key not in NOISY_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_normalize_result(item) for item in value]
    return value


def canonical_tool_signature_hash(
    tool_name: str,
    args: dict[str, Any],
    *,
    provider_id: str | None = None,
) -> str:
    # Guard decisions must be based on effect identity, not prompt text. This
    # hash is the runtime's canonical "what action was attempted" key.
    payload = {
        "tool_name": tool_name,
        "args": args,
        "provider_id": provider_id or "",
    }
    return hashlib.sha256(_stable_serialize(payload).encode("utf-8")).hexdigest()


def canonical_result_fingerprint(result: Any) -> str:
    normalized = _normalize_result(result)
    return hashlib.sha256(_stable_serialize(normalized).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolExecutionRecord:
    turn_id: str
    sequence: int
    tool_signature_hash: str
    result_fingerprint: str
    state_delta_hint: str = ""


@dataclass(frozen=True)
class ToolStagnationVerdict:
    status: str
    reason: str = ""
    matched_window: tuple[str, ...] = ()
    recommended_action: str = GuardAction.CONTINUE


@dataclass
class ToolStagnationGuardProcess:
    repeat_threshold: int = 3
    oscillation_window: int = 4
    history_by_turn: dict[str, list[ToolExecutionRecord]] = field(default_factory=dict)

    def observe_batch(
        self,
        turn_id: str,
        records: list[ToolExecutionRecord],
    ) -> ToolStagnationVerdict:
        # The guard is intentionally standalone so stagnation policy can evolve
        # without rewriting turn orchestration.
        if not records:
            return ToolStagnationVerdict(status=GuardStatus.OK)
        history = self.history_by_turn.setdefault(turn_id, [])
        history.extend(records)
        repeat_verdict = self._repeat_stagnation(history)
        if repeat_verdict is not None:
            return repeat_verdict
        oscillation_verdict = self._oscillation_stagnation(history)
        if oscillation_verdict is not None:
            return oscillation_verdict
        return ToolStagnationVerdict(status=GuardStatus.OK)

    def clear(self, turn_id: str) -> None:
        self.history_by_turn.pop(turn_id, None)

    def _repeat_stagnation(self, history: list[ToolExecutionRecord]) -> ToolStagnationVerdict | None:
        if len(history) < self.repeat_threshold:
            return None
        window = history[-self.repeat_threshold :]
        signature = window[0].tool_signature_hash
        fingerprint = window[0].result_fingerprint
        if all(
            item.tool_signature_hash == signature and item.result_fingerprint == fingerprint
            for item in window
        ):
            return ToolStagnationVerdict(
                status="repeat_stagnation",
                reason="identical tool signature and result repeated",
                matched_window=tuple(item.tool_signature_hash for item in window),
                recommended_action=GuardAction.TERMINATE_TOOL_LOOP,
            )
        return None

    def _oscillation_stagnation(self, history: list[ToolExecutionRecord]) -> ToolStagnationVerdict | None:
        if len(history) < self.oscillation_window:
            return None
        window = history[-self.oscillation_window :]
        signatures = [item.tool_signature_hash for item in window]
        fingerprints = {item.result_fingerprint for item in window}
        if len(set(signatures)) == 2 and signatures[0] == signatures[2] and signatures[1] == signatures[3] and len(fingerprints) <= 2:
            return ToolStagnationVerdict(
                status="oscillation_stagnation",
                reason="tool loop oscillates between conflicting signatures",
                matched_window=tuple(signatures),
                recommended_action=GuardAction.TERMINATE_TOOL_LOOP,
            )
        return None
