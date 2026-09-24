"""Logical execution identities shared by file authority and tool delivery."""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from pathlib import Path
from pal.execution.session_state import (
    InMemoryLogicalExecutionState, LogicalExecutionContext, LogicalExecutionStateBackend,
    DEFAULT_RESULT_RETENTION_USER_TURNS,
)

@dataclass
class LogicalExecutionSessions:
    retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS
    state_backend: LogicalExecutionStateBackend = field(
        default_factory=InMemoryLogicalExecutionState
    )
    _turn_contexts: dict[str, LogicalExecutionContext] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def begin_turn(
        self,
        *,
        runtime_root: Path | None,
        turn_id: str,
        scope_key: str = "",
        retention_user_turns: int | None = None,
        input_id: str = "",
    ) -> LogicalExecutionContext:
        resolved_retention = (
            self.retention_user_turns
            if retention_user_turns is None
            else max(1, int(retention_user_turns))
        )
        normalized_turn_id = str(turn_id or "").strip()
        session_id = str(scope_key or "").strip() or "local:default"
        semantic_input = str(input_id or "").strip() or normalized_turn_id or "input:default"
        context = self.state_backend.begin_input(
            execution_lifetime_id=session_id,
            input_id=semantic_input,
            retention_user_turns=resolved_retention,
        )
        with self._lock:
            if normalized_turn_id:
                self._turn_contexts[normalized_turn_id] = context
        _ = runtime_root
        return context

    def context_for_turn(self, turn_id: str | None) -> LogicalExecutionContext:
        normalized = str(turn_id or "").strip()
        if not normalized:
            raise ValueError("tool result state requires an explicit turn_id")
        with self._lock:
            context = self._turn_contexts.get(normalized)
        if context is not None:
            return self.state_backend.context(context.execution_lifetime_id)
        # Direct host invocations do not have a Core-owned L1 turn. Give that
        # explicit id an isolated lifetime; never borrow a previous context.
        return self.begin_turn(
            runtime_root=None,
            turn_id=normalized,
            scope_key=f"local:{normalized}",
            input_id=normalized,
        )

