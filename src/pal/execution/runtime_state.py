from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pal.execution.runtime import ExecutionRuntime
from pal.execution.session_state import (
    InMemoryLogicalExecutionState,
    LogicalExecutionContext,
)


EXECUTION_RUNTIME_STATE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class _PreparedExecutionState:
    backend: InMemoryLogicalExecutionState
    turn_contexts: dict[str, LogicalExecutionContext]


@dataclass
class ExecutionRuntimeStatePort:
    runtime: ExecutionRuntime
    module_id: str = "execution"
    schema_version: str = EXECUTION_RUNTIME_STATE_SCHEMA_VERSION
    state_order: int = 200

    def snapshot_state(self) -> Mapping[str, Any]:
        backend = self._backend()
        with self.runtime.tool_result_pager._lock:
            turn_contexts = {
                key: value.to_dict()
                for key, value in self.runtime.tool_result_pager._turn_contexts.items()
            }
        return {
            "logical_execution": backend.snapshot_state(),
            "turn_contexts": turn_contexts,
        }

    def prepare_restore_state(self, payload: Mapping[str, Any]) -> _PreparedExecutionState:
        value = dict(payload)
        logical_execution = value.get("logical_execution")
        turn_contexts = value.get("turn_contexts")
        if not isinstance(logical_execution, dict) or not isinstance(turn_contexts, dict):
            raise ValueError("execution runtime snapshot is invalid")
        backend = InMemoryLogicalExecutionState()
        prepared_backend = backend.prepare_restore_state(logical_execution)
        backend.install_prepared_state(prepared_backend)
        restored_contexts: dict[str, LogicalExecutionContext] = {}
        for key, raw in turn_contexts.items():
            turn_id = str(key or "").strip()
            if not turn_id or not isinstance(raw, dict):
                raise ValueError("execution runtime snapshot contains an invalid turn context")
            context = LogicalExecutionContext.from_dict(dict(raw))
            state = prepared_backend.get(context.execution_lifetime_id)
            if (
                state is None
                or not context.execution_lifetime_id
                or not context.input_id
                or context.current_user_turn > state.current_user_turn
                or context.context_epoch > state.context_epoch
            ):
                raise ValueError(
                    "execution runtime snapshot turn context has no owning lifetime"
                )
            if (
                state.current_user_turn
                >= context.current_user_turn + state.retention_user_turns
            ):
                continue
            restored_contexts[turn_id] = context
        return _PreparedExecutionState(
            backend=backend,
            turn_contexts=restored_contexts,
        )

    def install_prepared_state(self, prepared: _PreparedExecutionState) -> None:
        self.runtime.logical_state = prepared.backend
        with self.runtime.tool_result_pager._lock:
            self.runtime.tool_result_pager.state_backend = prepared.backend
            self.runtime.tool_result_pager._turn_contexts = prepared.turn_contexts

    def reset_state(self, reason: str) -> None:
        _ = reason
        self._backend().reset_state()
        with self.runtime.tool_result_pager._lock:
            self.runtime.tool_result_pager._turn_contexts.clear()

    def _backend(self) -> InMemoryLogicalExecutionState:
        backend = self.runtime.logical_state
        if not isinstance(backend, InMemoryLogicalExecutionState):
            raise TypeError(
                "execution runtime state snapshots require an in-process logical state backend"
            )
        return backend
