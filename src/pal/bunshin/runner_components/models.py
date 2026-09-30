from __future__ import annotations
from pal.shared.tool_protocol import ToolCallIR
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from pal.core.runtime_config import RuntimeConfig
from pal.core.runtime_state import RuntimeSnapshotCoordinator
from pal.foundation import EventEnvelope
from pal.memory import MemoryService
from pal.plugins.l3 import MockL3Plugin
from pal.shared import ChannelEnvelope, EndpointConfig, EventKind, ResponseHandle, SourceKind, ToolExecutionResult


@dataclass
class BunshinRuntimeBundle:
    llm_runtime: Any
    execution_runtime: Any
    memory_service: MemoryService
    module_registry: Any
    runtime_state_coordinator: RuntimeSnapshotCoordinator
    config: RuntimeConfig | None = None
    close_async: Callable[[], Awaitable[None]] | None = None
    memory_generation_id: str = ""

    async def close(self) -> None:
        if self.close_async is not None:
            await self.close_async()


@dataclass
class _BunshinFailureResult:
    user_feedback: str
    verification: Any = None
    report: Any = None


class _BunshinCooperativeCancel(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("summary") or payload.get("reason") or "bunshin cancellation requested"))
        self.payload = dict(payload)


class _BunshinCooperativeRestart(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("summary") or "bunshin suspended for manager restart"))
        self.payload = dict(payload)


class BunshinLLMRetryableError(RuntimeError):
    """Endpoint failure that must leave the logical role session resumable."""


@dataclass
class BunshinAgentLoopState:
    execution_runtime: "BunshinScopedExecutionRuntime"
    memory_service: MemoryService
    memory_candidate_sink: MockL3Plugin
    channel_envelope: ChannelEnvelope = field(
        default_factory=lambda: ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.BUNSHIN,
                payload={"text": ""},
            ),
            endpoint=EndpointConfig(endpoint_id="bunshin:unknown", channel_kind="stdio", binding_key="unknown"),
            response_handle=ResponseHandle(endpoint_id="bunshin:unknown"),
        )
    )
    pending_assistant_tool_text: str = ""
    pending_tool_call_batch: list[ToolCallIR] = field(default_factory=list)
    pending_tool_results: list[ToolExecutionResult] = field(default_factory=list)
    llm_round_count: int = 0
    tool_call_count: int = 0
    output_length_recovery_count: int = 0
    pending_output_length_recovery_note: str = ""


EventWriter = Callable[[dict[str, Any]], Awaitable[None]]


DecisionReader = Callable[[float | None], Awaitable[dict[str, Any] | None]]
