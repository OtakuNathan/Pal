from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pal.control import interactions as control_interactions
from pal.control.contracts import ControlAction, ControlDelivery, ControlRoute
from pal.control.routing import derive_control_scope_key, route_from_channel_envelope
from pal.core.contracts import CoreRuntimeState
from pal.core.runtime_config import RuntimeConfig
from pal.core.dispatcher import EventDispatcher
from pal.core.failure_orchestrator import FailureHandlingResult, FailureOrchestrator
from pal.core.main_context import MainContext
from pal.core.module_lifecycle import ModuleLifecycle
from pal.core.prompt_compiler import PromptCompiler
from pal.core.tool_stagnation import ToolStagnationGuardProcess
from pal.core.tool_surface import ToolSurface
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import EffectRequest, EffectResult, TurnContinuation, TurnOutcome, L1CommitPayload, channel_turn_program
from pal.core.module_registry import ModuleHandle
from pal.execution import CapabilityCall
from pal.execution.contracts import CapabilityResult
from pal.foundation import AttachmentSpec, EventEnvelope, utc_now
from pal.failure import FailureSignal, FailureUserFeedback
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.memory import L1MessageKind, L1TranscriptMessage, MemoryCommitRequest
from pal.memory.candidates import l3_commit_args_from_memory_candidate
from pal.shared import ChannelEnvelope, EventKind, SourceKind
from pal.shared import IntrospectionPort, PromptAssemblyContext, PromptFragment, RuntimeStatus, llm_tool_name
from pal.shared.payloads import extract_text_from_payload


@dataclass
class TurnManager:
    context: MainContext
    state: CoreRuntimeState
    guard: ToolStagnationGuardProcess = field(default_factory=ToolStagnationGuardProcess)
    config: RuntimeConfig = field(default_factory=RuntimeConfig.defaults)

    def start(self, channel_envelope: ChannelEnvelope) -> TurnContinuation:
        turn_id = channel_envelope.event.event_id
        max_output_tokens = self._resolve_max_output_tokens()
        control_scope_key = derive_control_scope_key(
            endpoint_id=channel_envelope.endpoint.endpoint_id,
            channel_kind=channel_envelope.endpoint.channel_kind,
            reply_target=channel_envelope.response_handle.reply_target,
            payload=channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {},
        )
        scope_state = self._ensure_scope_state(control_scope_key)
        continuation = TurnContinuation(
            turn_id=turn_id,
            channel_envelope=channel_envelope,
            program=channel_turn_program(channel_envelope, core_mode=self.state.mode, max_output_tokens=max_output_tokens),
            correlation_id=channel_envelope.event.correlation_id or turn_id,
            control_scope_key=control_scope_key,
            turn_settings_snapshot=self._build_turn_settings_snapshot(),
        )
        self.state.active_turns[turn_id] = continuation
        self.state.turn_scopes[turn_id] = control_scope_key
        self._remember_active_turn(scope_state, turn_id)
        return continuation

    def _resolve_max_output_tokens(self, *, preferred_endpoint_id: str | None = None) -> int:
        llm_runtime = self.context.port_registry.get("llm:llm")
        if llm_runtime is not None:
            fn = getattr(llm_runtime, "resolve_max_output_tokens", None)
            if callable(fn):
                try:
                    result = fn(preferred_endpoint_id=preferred_endpoint_id)
                except TypeError:
                    result = fn()
                if result is not None:
                    return result
        return self.config.fallback_max_output_tokens

    def resume(
        self,
        continuation: TurnContinuation,
        result: EffectResult | None = None,
    ) -> EffectRequest | TurnOutcome:
        try:
            if not continuation.started:
                effect = next(continuation.program)
                continuation.started = True
            else:
                effect = continuation.program.send(result or EffectResult(status=RuntimeStatus.OK))
            continuation.waiting_effect_id = effect.effect_id
            return effect
        except StopIteration as stop:
            self.state.active_turns.pop(continuation.turn_id, None)
            outcome = stop.value
            self.state.completed_turns[continuation.turn_id] = outcome
            self.guard.clear(continuation.turn_id)
            self._mark_turn_exited(continuation.turn_id)
            return outcome

    async def interrupt_by_scope(self, control_scope_key: str, *, reason: str = "interrupted") -> bool:
        scope_state = self._ensure_scope_state(control_scope_key)
        async with scope_state.interrupt_lock:
            turn_id = self.latest_active_turn_id(control_scope_key)
            if not turn_id:
                return False
            in_flight = scope_state.interrupt_task
            if scope_state.interrupting_turn_id == turn_id and in_flight is not None and not in_flight.done():
                interrupt_task = in_flight
            else:
                interrupt_task = asyncio.create_task(
                    self._interrupt_turn_async(
                        control_scope_key,
                        turn_id,
                        reason=reason,
                    )
                )
                scope_state.interrupting_turn_id = turn_id
                scope_state.interrupt_task = interrupt_task
        return await interrupt_task

    async def _interrupt_turn_async(self, control_scope_key: str, turn_id: str, *, reason: str) -> bool:
        scope_state = self._ensure_scope_state(control_scope_key)
        current_task = asyncio.current_task()
        try:
            continuation = self.state.active_turns.get(turn_id)
            if isinstance(continuation, TurnContinuation):
                continuation.interrupted = True
                continuation.interrupt_reason = reason
                await self.commit_l1_exit_checkpoint_async(
                    continuation,
                    kind=L1MessageKind.TURN_INTERRUPTED,
                    status="interrupted",
                    reason=reason,
                )
            output_port = self.context.port_registry.get("agent_io:output") or self.context.port_registry.get("channel:channel")
            if output_port is not None and isinstance(continuation, TurnContinuation):
                abort_result = output_port.abort_stream(continuation.channel_envelope.response_handle, reason=reason)
                if inspect.isawaitable(abort_result):
                    await abort_result
            execution_runtime = getattr(self.context, "execution_runtime", None)
            if execution_runtime is not None:
                interrupt_turn = getattr(execution_runtime, "interrupt_turn", None)
                if callable(interrupt_turn):
                    await interrupt_turn(turn_id)
            task = self.state.turn_tasks.get(turn_id)
            if task is not None and not task.done():
                task.cancel()
            return True
        finally:
            async with scope_state.interrupt_lock:
                if scope_state.interrupt_task is current_task:
                    scope_state.interrupt_task = None
                    if scope_state.interrupting_turn_id == turn_id:
                        scope_state.interrupting_turn_id = None

    def cleanup_interrupted(self, turn_id: str, *, reason: str = "interrupted") -> None:
        continuation = self.state.active_turns.pop(turn_id, None)
        if isinstance(continuation, TurnContinuation):
            continuation.interrupted = True
            continuation.interrupt_reason = reason
        self.guard.clear(turn_id)
        self._mark_turn_exited(turn_id)

    async def commit_l1_exit_checkpoint_async(
        self,
        continuation: TurnContinuation,
        *,
        kind: L1MessageKind,
        status: str,
        reason: str,
    ) -> None:
        if continuation.l1_exit_checkpoint_committed:
            return
        transcript = self._build_l1_exit_checkpoint_transcript(
            continuation,
            kind=kind,
            status=status,
            reason=reason,
        )
        if not transcript:
            return
        continuation.l1_exit_checkpoint_committed = True
        memory_service = self.context.port_registry.get("memory:memory")
        if memory_service is None:
            return
        try:
            request = MemoryCommitRequest(
                turn_id=continuation.turn_id,
                transcript=transcript,
                metadata={"exit_checkpoint_status": status},
            )
            async_method = getattr(memory_service, "acommit_l1", None)
            if callable(async_method):
                result = async_method(request)
                if inspect.isawaitable(result):
                    result = await result
            else:
                sync_method = getattr(memory_service, "commit_l1")
                result = await asyncio.to_thread(sync_method, request)
        except Exception as exc:
            self.state.diagnostics.append(
                {
                    "kind": "memory.exit_checkpoint.failed",
                    "turn_id": continuation.turn_id,
                    "status": status,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            return
        if getattr(result, "status", RuntimeStatus.OK) != RuntimeStatus.OK:
            self.state.diagnostics.append(
                {
                    "kind": "memory.exit_checkpoint.retry",
                    "turn_id": continuation.turn_id,
                    "status": getattr(result, "status", ""),
                    "exit_status": status,
                }
            )

    def _build_l1_exit_checkpoint_transcript(
        self,
        continuation: TurnContinuation,
        *,
        kind: L1MessageKind,
        status: str,
        reason: str,
    ) -> list[L1TranscriptMessage]:
        transcript: list[L1TranscriptMessage] = []
        user_text = extract_text_from_payload(continuation.channel_envelope.event.payload).strip()
        if user_text:
            transcript.append(L1TranscriptMessage(role="user", content=user_text, kind=L1MessageKind.USER_REQUEST))

        protocol_transcript, protocol_assistant_contents = self._build_l1_tool_protocol_transcript(
            continuation,
            diagnostic_kind="memory.exit_checkpoint.tool_protocol_invalid",
        )
        transcript.extend(protocol_transcript)

        for text in continuation.emitted_reply_texts:
            rendered = str(text or "").strip()
            if not rendered:
                continue
            if rendered in protocol_assistant_contents:
                continue
            transcript.append(L1TranscriptMessage(role="assistant", content=rendered, kind=L1MessageKind.ASSISTANT_REPLY))

        summary = self._render_l1_exit_checkpoint_summary(
            continuation,
            kind=kind,
            status=status,
            reason=reason,
            user_text=user_text,
        )
        transcript.append(L1TranscriptMessage(role="assistant", content=summary, kind=kind))
        return transcript

    def _render_l1_exit_checkpoint_summary(
        self,
        continuation: TurnContinuation,
        *,
        kind: L1MessageKind,
        status: str,
        reason: str,
        user_text: str,
    ) -> str:
        lines = [
            f'<turn_checkpoint kind="{kind.value}">',
            "This is recovery context from a previous turn, not a new user request.",
            f"turn_id: {continuation.turn_id}",
            f"status: {status}",
        ]
        reason_text = self._truncate_checkpoint_text(reason, max_chars=240)
        if reason_text:
            lines.append(f"reason: {reason_text}")
        user_preview = self._truncate_checkpoint_text(user_text, max_chars=360)
        if user_preview:
            lines.append(f"user_request: {user_preview}")
        if continuation.tool_observations:
            lines.append("completed_tools:")
            for observation in continuation.tool_observations[:8]:
                ok = "ok" if getattr(observation, "ok", False) else "error"
                summary = self._truncate_checkpoint_text(getattr(observation, "summary", ""), max_chars=220)
                lines.append(f"- {getattr(observation, 'tool_name', '')} ({ok}): {summary}")
            if len(continuation.tool_observations) > 8:
                lines.append(f"- ... +{len(continuation.tool_observations) - 8} more")
        else:
            lines.append("completed_tools: none recorded")
        if continuation.pending_tool_call_batch:
            lines.append(f"pending_tool_batch: {len(continuation.pending_tool_call_batch)} tool call(s) were not fully recorded")
        lines.append("turn_outcome: not committed")
        lines.append("</turn_checkpoint>")
        return "\n".join(lines)

    def _build_l1_tool_protocol_transcript(
        self,
        continuation: TurnContinuation,
        *,
        diagnostic_kind: str,
    ) -> tuple[list[L1TranscriptMessage], list[str]]:
        messages = list(continuation.tool_protocol_messages)
        if not messages:
            return [], []
        validation_error = self._tool_protocol_validation_error(messages)
        if validation_error:
            self.state.diagnostics.append(
                {
                    "kind": diagnostic_kind,
                    "turn_id": continuation.turn_id,
                    "error": validation_error,
                }
            )
            return [], []
        transcript: list[L1TranscriptMessage] = []
        protocol_assistant_contents: list[str] = []
        for msg in messages:
            role = str(msg.get("role", "") or "").strip()
            content = str(msg.get("content", "") or "")
            if role == "tool":
                content = self._truncate_tool_result_for_l1(content)
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and tool_calls:
                protocol_assistant_contents.append(content.strip())
            transcript.append(
                L1TranscriptMessage(
                    role=role,
                    content=content,
                    kind=L1MessageKind.TOOL_RESULT if role == "tool" else L1MessageKind.ASSISTANT_TOOL_CALL,
                    tool_calls=tool_calls if isinstance(tool_calls, list) else None,
                    tool_call_id=msg.get("tool_call_id"),
                )
            )
        return transcript, protocol_assistant_contents

    @staticmethod
    def _tool_protocol_validation_error(messages: list[dict[str, Any]]) -> str:
        pending_tool_ids: set[str] = set()
        pending_assistant_index: int | None = None
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                return f"tool protocol message at index {index} is not an object"
            role = str(message.get("role", "") or "").strip()
            if pending_tool_ids and role != "tool":
                return (
                    f"assistant tool_calls at index {pending_assistant_index} missing tool results: "
                    f"{', '.join(sorted(pending_tool_ids))}"
                )
            if role == "assistant" and message.get("tool_calls"):
                tool_call_ids = TurnManager._extract_tool_protocol_call_ids(message.get("tool_calls"))
                if not tool_call_ids:
                    return f"assistant tool_calls at index {index} do not have complete ids"
                if len(tool_call_ids) != len(set(tool_call_ids)):
                    return f"assistant tool_calls at index {index} contain duplicate ids"
                pending_tool_ids = set(tool_call_ids)
                pending_assistant_index = index
                continue
            if role == "tool":
                tool_call_id = str(message.get("tool_call_id") or "").strip()
                if not tool_call_id:
                    return f"tool message at index {index} is missing tool_call_id"
                if not pending_tool_ids:
                    return f"tool message at index {index} has no preceding assistant tool_calls"
                if tool_call_id not in pending_tool_ids:
                    return f"tool message at index {index} has unexpected tool_call_id {tool_call_id}"
                pending_tool_ids.remove(tool_call_id)
                continue
            return f"tool protocol message at index {index} has unexpected role {role}"
        if pending_tool_ids:
            return (
                f"assistant tool_calls at index {pending_assistant_index} missing tool results: "
                f"{', '.join(sorted(pending_tool_ids))}"
            )
        return ""

    @staticmethod
    def _extract_tool_protocol_call_ids(tool_calls: object) -> list[str]:
        if not isinstance(tool_calls, list) or not tool_calls:
            return []
        ids: list[str] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                return []
            call_id = str(tool_call.get("id") or "").strip()
            if not call_id:
                return []
            ids.append(call_id)
        return ids

    def _truncate_tool_result_for_l1(self, content: str) -> str:
        max_chars = max(0, int(getattr(self.config, "l1_tool_result_max_chars", 8_000) or 0))
        if not max_chars or len(content) <= max_chars:
            return content
        configured_preview = int(getattr(self.config, "l1_tool_result_preview_chars", 4_000) or 0)
        preview_chars = max(1, min(configured_preview or max_chars, max_chars))
        preview = content[:preview_chars].rstrip()
        return f"{preview}\n\n[... truncated, original: {len(content)} chars]"

    @staticmethod
    def _truncate_checkpoint_text(value: object, *, max_chars: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= max_chars:
            return text
        return f"{text[: max(1, max_chars - 18)].rstrip()}...[truncated]"

    def _build_turn_settings_snapshot(self) -> dict[str, Any]:
        llm_runtime = self.context.port_registry.get("llm:llm")
        think_level = None
        if llm_runtime is not None:
            refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
            think_level = str(getattr(llm_runtime, "think_level", "") or "").strip() or None
        return {
            "think_level": think_level or "balanced",
            "prompt_log_enabled": bool(self.state.prompt_log_enabled),
        }

    def latest_active_turn_id(self, control_scope_key: str) -> str | None:
        scope_state = self._ensure_scope_state(control_scope_key)
        turn_id = scope_state.active_turn_id
        if turn_id and self._is_turn_live(turn_id):
            scope_state.drained_event.clear()
            return turn_id
        scope_state.active_turn_id = None
        scope_state.drained_event.set()
        return None

    def _remember_active_turn(self, scope_state, turn_id: str) -> None:
        scope_state.active_turn_id = turn_id
        scope_state.drained_event.clear()

    def _is_turn_live(self, turn_id: str) -> bool:
        task = self.state.turn_tasks.get(turn_id)
        if task is not None and not task.done():
            return True
        return turn_id in self.state.active_turns

    def _ensure_scope_state(self, control_scope_key: str):
        scope_state = self.state.control_scopes.get(control_scope_key)
        if scope_state is None:
            from pal.core.contracts import ControlScopeState

            scope_state = ControlScopeState()
            self.state.control_scopes[control_scope_key] = scope_state
        return scope_state

    def _mark_turn_exited(self, turn_id: str) -> None:
        control_scope_key = self.state.turn_scopes.pop(turn_id, None)
        if not control_scope_key:
            return
        scope_state = self._ensure_scope_state(control_scope_key)
        if scope_state.active_turn_id == turn_id:
            scope_state.active_turn_id = None
            scope_state.drained_event.set()


@dataclass
class MainLoop:
    queue: deque[EventEnvelope] = field(default_factory=deque)
    dispatcher: EventDispatcher = field(default_factory=EventDispatcher)
    _wakeup_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _wakeup_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    def enqueue(self, envelope: EventEnvelope) -> None:
        self.queue.append(envelope)
        self.notify_ready()

    def pop(self) -> EventEnvelope | None:
        if not self.queue:
            return None
        return self.queue.popleft()

    def bind_async_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._wakeup_loop is loop and self._wakeup_event is not None:
            return
        self._wakeup_loop = loop
        self._wakeup_event = asyncio.Event()

    def notify_ready(self) -> None:
        event = self._wakeup_event
        loop = self._wakeup_loop
        if event is None or loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(event.set)

    async def wait_for_ready_async(self, *, timeout: float | None = None) -> None:
        self.bind_async_loop()
        event = self._wakeup_event
        if event is None:
            return
        if self.queue:
            return
        if event.is_set():
            event.clear()
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        finally:
            event.clear()

    def drain_ready_sources(self, context: MainContext) -> list[EventEnvelope]:
        drained: list[EventEnvelope] = []
        for source in context.event_source_registry.iter_sources():
            if not source.prepare(context):
                continue
            events = source.drain(context)
            for envelope in events:
                self.enqueue(envelope)
            drained.extend(events)
        return drained

    def run_once(self, context: MainContext, state: CoreRuntimeState) -> EventEnvelope | None:
        return asyncio.run(self.run_once_async(context, state))

    async def run_once_async(self, context: MainContext, state: CoreRuntimeState) -> EventEnvelope | None:
        core_port = context.port_registry.get("core:core")
        expire = getattr(core_port, "expire_pending_control_requests_async", None)
        if callable(expire):
            result = expire()
            if inspect.isawaitable(result):
                await result
        self.drain_ready_sources(context)
        envelope = self.pop()
        if envelope is None:
            return None
        for derived in await self.dispatcher.dispatch_async(envelope, context):
            self.enqueue(derived)
        return envelope

    def run_until_idle(self, context: MainContext, state: CoreRuntimeState, *, max_iterations: int = 64) -> list[EventEnvelope]:
        processed: list[EventEnvelope] = []
        for _ in range(max_iterations):
            envelope = self.run_once(context, state)
            if envelope is None:
                self._cleanup_finished_turn_tasks(state)
                self.drain_ready_sources(context)
                if self.queue:
                    continue
                break
            processed.append(envelope)
        return processed

    async def run_until_idle_async(self, context: MainContext, state: CoreRuntimeState, *, max_iterations: int = 64) -> list[EventEnvelope]:
        processed: list[EventEnvelope] = []
        for _ in range(max_iterations):
            envelope = await self.run_once_async(context, state)
            if envelope is None:
                pending = self._cleanup_finished_turn_tasks(state)
                if not pending:
                    self.drain_ready_sources(context)
                    if self.queue:
                        continue
                    break
                wake_task = asyncio.create_task(self.wait_for_ready_async())
                try:
                    await asyncio.wait([*pending, wake_task], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not wake_task.done():
                        wake_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await wake_task
                continue
            processed.append(envelope)
        return processed

    def _cleanup_finished_turn_tasks(self, state: CoreRuntimeState) -> list[asyncio.Task[Any]]:
        pending: list[asyncio.Task[Any]] = []
        for turn_id, task in list(state.turn_tasks.items()):
            if task is None:
                state.turn_tasks.pop(turn_id, None)
                continue
            if not task.done():
                pending.append(task)
                continue
            if state.turn_tasks.get(turn_id) is task:
                state.turn_tasks.pop(turn_id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
        return pending


EventLoop = MainLoop


@dataclass
class CoreTurnIOPort:
    core: "PalCore"

    async def send_attachment_for_turn(self, turn_id: str | None, attachment: AttachmentSpec) -> CapabilityResult:
        return await self.core.send_attachment_for_turn(turn_id, attachment)

    def artifact_scope_for_turn(self, turn_id: str | None) -> str | None:
        return self.core.artifact_scope_for_turn(turn_id)


@dataclass
class PalCore:
    context: MainContext = field(default_factory=MainContext)
    state: CoreRuntimeState = field(default_factory=CoreRuntimeState)
    config: RuntimeConfig = field(default_factory=RuntimeConfig.defaults)
    main_loop: MainLoop = field(default_factory=MainLoop)
    turn_manager: TurnManager = field(init=False)
    prompt_compiler: PromptCompiler = field(init=False)
    tool_surface: ToolSurface = field(init=False)
    failure_orchestrator: FailureOrchestrator = field(init=False)
    turn_executor: TurnExecutor = field(init=False)
    module_lifecycle: ModuleLifecycle = field(init=False)

    def __post_init__(self) -> None:
        self.turn_manager = TurnManager(
            context=self.context,
            state=self.state,
            guard=ToolStagnationGuardProcess.from_config(self.config),
            config=self.config,
        )
        self.prompt_compiler = PromptCompiler(self.context)
        self.tool_surface = ToolSurface(self.context)
        self.module_lifecycle = ModuleLifecycle(self.context, self.state)
        self.context.execution_runtime.lifecycle_controller = self
        self.failure_orchestrator = FailureOrchestrator(
            self.context,
            call_port_async=self._call_port_async,
            build_canonical_prompt=self.build_canonical_prompt,
            debug_log_prompt=self._debug_log_prompt,
            tool_surface=self.tool_surface,
        )
        self.turn_executor = TurnExecutor(
            self.context,
            self.state,
            self.turn_manager,
            call_port_async=self._call_port_async,
            build_canonical_prompt=self.build_canonical_prompt,
            debug_log_prompt=self._debug_log_prompt,
            debug_log_outcome=self._debug_log_outcome,
            debug_log_reply=self._debug_log_reply,
            build_llm_tool_contracts=self._build_llm_tool_contracts,
            handle_failure_async=self.handle_failure_async,
            render_failure_feedback_text=self._render_failure_feedback_text,
            should_enter_failure_flow_for_tool_result=self._should_enter_failure_flow_for_tool_result,
            config=self.config,
        )
        self.context.execution_runtime.register_provider_ref("core:turn_io", CoreTurnIOPort(core=self))

    def event_loop(self) -> MainLoop:
        return self.main_loop

    def receive_event(self, envelope: EventEnvelope) -> None:
        self.main_loop.enqueue(envelope)

    def notify_ready(self) -> None:
        self.main_loop.notify_ready()

    def bind_async_wakeup_sources(self) -> None:
        self.main_loop.bind_async_loop()
        channel_runtime = self.context.port_registry.get("channel:channel")
        if channel_runtime is not None and hasattr(channel_runtime, "on_ready"):
            channel_runtime.on_ready = self.notify_ready
        proactive_manager = self.context.port_registry.get("proactive:proactive_manager")
        if proactive_manager is not None and hasattr(proactive_manager, "on_ready"):
            proactive_manager.on_ready = self.notify_ready
        minion_provider = self.context.port_registry.get("minion:minion")
        if minion_provider is not None and hasattr(minion_provider, "event_notify"):
            minion_provider.event_notify = self.notify_ready

    async def wait_for_ready_async(self, *, timeout: float | None = None) -> None:
        await self.main_loop.wait_for_ready_async(timeout=timeout)

    def next_wakeup_timeout_seconds(self) -> float | None:
        candidates: list[float] = []
        proactive_manager = self.context.port_registry.get("proactive:proactive_manager")
        seconds_until_next_due = getattr(proactive_manager, "seconds_until_next_due", None)
        if callable(seconds_until_next_due):
            service_timeout = seconds_until_next_due()
            if service_timeout is not None:
                candidates.append(float(service_timeout))
        control_timeout = self._seconds_until_next_control_request_expiry()
        if control_timeout is not None:
            candidates.append(control_timeout)
        return min(candidates) if candidates else None

    def _seconds_until_next_control_request_expiry(self) -> float | None:
        now = datetime.now(timezone.utc)
        nearest: float | None = None
        for scope_state in self.state.control_scopes.values():
            for request in getattr(scope_state, "pending_requests", {}).values():
                expires_at = _parse_utc_timestamp(getattr(request, "expires_at", ""))
                delay = max(0.0, (expires_at - now).total_seconds())
                nearest = delay if nearest is None else min(nearest, delay)
        return nearest

    def drain_once(self) -> EventEnvelope | None:
        return asyncio.run(self.drain_once_async())

    async def drain_once_async(self) -> EventEnvelope | None:
        return await self.main_loop.run_once_async(self.context, self.state)

    def run_until_idle(self, *, max_iterations: int = 64) -> list[EventEnvelope]:
        return asyncio.run(self.run_until_idle_async(max_iterations=max_iterations))

    async def run_until_idle_async(self, *, max_iterations: int = 64) -> list[EventEnvelope]:
        return await self.main_loop.run_until_idle_async(self.context, self.state, max_iterations=max_iterations)

    async def send_attachment_for_turn(self, turn_id: str | None, attachment: AttachmentSpec) -> CapabilityResult:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="turn_id is required",
                llm_text="Could not send attachment: turn_id is required.",
                structured={"reason": "turn_id_required"},
            )
        continuation = self.state.active_turns.get(normalized_turn_id)
        if not isinstance(continuation, TurnContinuation):
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="active turn not found",
                llm_text="Could not send attachment: active turn not found.",
                structured={"reason": "turn_not_active", "turn_id": normalized_turn_id},
            )
        path = Path(attachment.path).expanduser()
        if not path.is_file():
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text=f"attachment file not found: {path}",
                llm_text=f"Could not send attachment: file not found at {path}.",
                structured={"reason": "file_not_found", "path": str(path)},
            )
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        channel_runtime = self.context.port_registry.get("channel:channel")
        queue_attachment = getattr(channel_runtime, "queue_attachment", None)
        if not callable(queue_attachment):
            return CapabilityResult(
                status=RuntimeStatus.UNSUPPORTED,
                text="channel runtime does not support attachments",
                llm_text="Could not send attachment: channel runtime does not support attachments.",
                structured={"reason": "attachment_not_supported"},
            )
        normalized = AttachmentSpec(
            path=str(resolved),
            caption=str(attachment.caption or ""),
            file_name=str(attachment.file_name or resolved.name),
            mime_type=str(attachment.mime_type or ""),
        )
        attachment_id = queue_attachment(continuation.channel_envelope, normalized)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"queued attachment: {normalized.file_name}",
            llm_text=f"Queued attachment for delivery: {normalized.file_name}.",
            structured={
                "attachment_id": attachment_id,
                "path": normalized.path,
                "file_name": normalized.file_name,
                "mime_type": normalized.mime_type,
            },
        )

    def _derive_channel_control_scope_key(self, channel_envelope: ChannelEnvelope) -> str:
        return derive_control_scope_key(
            endpoint_id=channel_envelope.endpoint.endpoint_id,
            channel_kind=channel_envelope.endpoint.channel_kind,
            reply_target=channel_envelope.response_handle.reply_target,
            payload=channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {},
        )

    def _turn_task_running(self, turn_id: str) -> bool:
        task = self.state.turn_tasks.get(turn_id)
        return task is not None and not task.done()

    def _channel_turn_is_pending(self, scope_state, turn_id: str) -> bool:
        return any(
            getattr(getattr(envelope, "event", None), "event_id", None) == turn_id
            for envelope in scope_state.pending_channel_turns
        )

    def _queue_channel_status(self, channel_envelope: ChannelEnvelope, kind: str, payload: dict[str, Any] | None = None) -> None:
        channel_runtime = self.context.port_registry.get("channel:channel")
        if channel_runtime is not None:
            channel_runtime.queue_status(channel_envelope, kind, payload=dict(payload or {}))

    def _start_channel_turn_task_locked(
        self,
        channel_envelope: ChannelEnvelope,
        control_scope_key: str,
        scope_state,
        *,
        emit_typing_start: bool = False,
    ) -> asyncio.Task[Any]:
        turn_id = channel_envelope.event.event_id
        if emit_typing_start:
            self._queue_channel_status(channel_envelope, "typing_start")
        self.context.turn_event_bus.emit("turn.start", {
            "turn_id": turn_id,
            "scope_key": control_scope_key,
            "endpoint_id": channel_envelope.endpoint.endpoint_id,
            "channel_kind": channel_envelope.endpoint.channel_kind,
            "reply_target": dict(channel_envelope.response_handle.reply_target),
        })
        scope_state.active_turn_id = turn_id
        scope_state.drained_event.clear()
        self.state.turn_scopes[turn_id] = control_scope_key
        task = asyncio.create_task(self._background_channel_turn_runner_async(channel_envelope))
        self.state.turn_tasks[turn_id] = task
        task.add_done_callback(lambda finished, current_turn_id=turn_id: self._on_turn_task_done(current_turn_id, finished))
        return task

    async def _start_next_queued_turn_async(self, control_scope_key: str) -> None:
        scope_state = self._ensure_scope_state(control_scope_key)
        async with scope_state.transition_lock:
            if scope_state.quiescing:
                return
            if self.turn_manager.latest_active_turn_id(control_scope_key) is not None:
                return
            while scope_state.pending_channel_turns:
                next_envelope = scope_state.pending_channel_turns.popleft()
                next_turn_id = next_envelope.event.event_id
                if self._turn_task_running(next_turn_id):
                    continue
                self._start_channel_turn_task_locked(
                    next_envelope,
                    control_scope_key,
                    scope_state,
                    emit_typing_start=True,
                )
                return

    async def schedule_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> None:
        control_scope_key = self._derive_channel_control_scope_key(channel_envelope)
        channel_envelope = await self._prepare_channel_artifacts_async(channel_envelope, control_scope_key)
        scope_state = self._ensure_scope_state(control_scope_key)
        turn_id = channel_envelope.event.event_id
        should_reject = False
        should_stop_only = False
        should_queue = False
        async with scope_state.transition_lock:
            if scope_state.quiescing:
                should_reject = True
            elif self._turn_task_running(turn_id) or self._channel_turn_is_pending(scope_state, turn_id):
                should_stop_only = True
            elif self.turn_manager.latest_active_turn_id(control_scope_key) is not None:
                scope_state.pending_channel_turns.append(channel_envelope)
                should_queue = True
            else:
                self._start_channel_turn_task_locked(channel_envelope, control_scope_key, scope_state)
        if should_reject:
            await self._reply_to_route_async(
                self._route_from_channel_envelope(channel_envelope),
                "This scope is resetting. Please retry in a moment.",
            )
            await self._status_to_route_async(
                self._route_from_channel_envelope(channel_envelope),
                "working_stop",
                {},
            )
            return
        if should_stop_only or should_queue:
            await self._status_to_route_async(
                self._route_from_channel_envelope(channel_envelope),
                "working_stop",
                {},
            )
            return

    def process_channel_turn(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        return asyncio.run(self.process_channel_turn_async(channel_envelope))

    async def process_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        control_scope_key = self._derive_channel_control_scope_key(channel_envelope)
        scope_state = self._ensure_scope_state(control_scope_key)
        if scope_state.quiescing:
            raise RuntimeError("scope is quiescing")
        # The hot path is: start turn -> interpret yielded effects -> resume
        # until the generator returns a TurnOutcome.
        continuation = self.turn_manager.start(channel_envelope)
        self._begin_tool_result_turn(continuation)
        return await self._run_turn_continuation_async(continuation)

    def turn_execution_options(self) -> dict[str, Any]:
        return {
            "core_mode": self.state.mode,
            "max_output_tokens": self.turn_manager._resolve_max_output_tokens(),
        }

    def track_turn_task(self, continuation: TurnContinuation, task: asyncio.Task[Any]) -> None:
        self.state.active_turns[continuation.turn_id] = continuation
        self.state.turn_tasks[continuation.turn_id] = task
        task.add_done_callback(lambda finished, current_turn_id=continuation.turn_id: self._on_turn_task_done(current_turn_id, finished))

    async def run_turn_continuation_async(self, continuation: TurnContinuation) -> TurnOutcome:
        self.state.active_turns[continuation.turn_id] = continuation
        self._begin_tool_result_turn(continuation)
        return await self._run_turn_continuation_async(continuation)

    def _begin_tool_result_turn(self, continuation: TurnContinuation) -> None:
        begin = getattr(self.context.execution_runtime, "begin_tool_result_turn", None)
        if not callable(begin):
            return
        begin(
            turn_id=continuation.turn_id,
            scope_key=continuation.control_scope_key,
            retention_user_turns=getattr(self.config, "tool_result_pager_retention_user_turns", 5),
        )

    async def _background_channel_turn_runner_async(self, channel_envelope: ChannelEnvelope) -> None:
        turn_id = channel_envelope.event.event_id
        control_scope_key = self._derive_channel_control_scope_key(channel_envelope)
        turn_status = "success"
        try:
            await self.process_channel_turn_async(channel_envelope)
        except asyncio.CancelledError:
            turn_status = "interrupted"
            self.turn_manager.cleanup_interrupted(turn_id, reason="interrupted")
        except Exception as exc:
            turn_status = "failed"
            self.turn_manager.cleanup_interrupted(turn_id, reason="failed")
            self.state.diagnostics.append(
                {
                    "kind": "turn.background.failed",
                    "turn_id": turn_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        finally:
            if self.state.turn_scopes.get(turn_id) == control_scope_key and turn_id not in self.state.active_turns:
                self.turn_manager._mark_turn_exited(turn_id)
            self.context.turn_event_bus.emit("turn.end", {
                "turn_id": turn_id,
                "scope_key": control_scope_key,
                "endpoint_id": channel_envelope.endpoint.endpoint_id,
                "channel_kind": channel_envelope.endpoint.channel_kind,
                "reply_target": dict(channel_envelope.response_handle.reply_target),
                "status": turn_status,
            })
            self._queue_channel_status(channel_envelope, "working_stop")
            await self._start_next_queued_turn_async(control_scope_key)

    def _on_turn_task_done(self, turn_id: str, task: asyncio.Task[Any]) -> None:
        stored = self.state.turn_tasks.get(turn_id)
        if stored is task:
            self.state.turn_tasks.pop(turn_id, None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        self.notify_ready()

    async def handle_control_action_async(self, action: ControlAction) -> None:
        await self.expire_pending_control_requests_async()
        status_route = action.route or (action.delivery.route if action.delivery is not None else None)
        try:
            if action.delivery is not None:
                await self._deliver_control_delivery_async(action.delivery, fallback_route=action.route)
                return
            if action.action_kind == "show_panel":
                await self._handle_show_panel_async(action)
                return
            if action.action_kind == "show_think":
                await self._handle_show_think_async(action)
                return
            if action.action_kind == "set_think":
                await self._handle_set_think_async(action)
                return
            if action.action_kind == "show_log":
                await self._handle_show_log_async(action)
                return
            if action.action_kind == "set_log":
                await self._handle_set_log_async(action)
                return
            if action.action_kind == "interrupt_turn":
                await self._handle_interrupt_turn_async(action)
                return
            if action.action_kind == "open_reset_confirm":
                await self._handle_open_reset_confirm_async(action)
                return
            if action.action_kind == "cancel_reset_confirm":
                await self._handle_cancel_reset_confirm_async(action)
                return
            if action.action_kind == "reset_memory":
                await self._handle_reset_memory_async(action)
                return
            if action.action_kind == "compact_memory":
                await self._handle_compact_memory_async(action)
                return
            if action.action_kind == "memory_candidate_decision":
                await self._handle_memory_candidate_decision_async(action)
                return
            if action.action_kind == "refresh_llm_endpoint":
                await self._handle_refresh_llm_endpoint_async(action)
                return
            if action.action_kind == "refresh_tool_surface":
                await self._handle_refresh_tool_surface_async(action)
                return
            if action.action_kind == "route_reply":
                await self._handle_route_reply_async(action)
                return
            if action.action_kind == "invoke_capability":
                await self._handle_invoke_capability_async(action)
                return
            handled = await self.context.control_action_registry.handle(action)
            if handled.handled:
                delivery = handled.structured.get("delivery") if isinstance(handled.structured, dict) else None
                if isinstance(delivery, ControlDelivery):
                    await self._deliver_control_delivery_async(delivery, fallback_route=action.route)
                    return
                message = handled.message.strip()
                if message:
                    await self._deliver_control_delivery_async(
                        control_interactions.terminal_delivery_for_action(action, message)
                    )
                return
            if action.action_kind == "invalid_command":
                await self._deliver_control_delivery_async(
                    control_interactions.terminal_delivery_for_action(action, action.notes or "Invalid command.")
                )
                return
            if action.action_kind == "unknown_command":
                command_name = str(action.args.get("command_name") or "").strip()
                text = f"Unknown command: /{command_name}" if command_name else "Unknown command."
                await self._deliver_control_delivery_async(control_interactions.terminal_delivery_for_action(action, text))
                return
            await self._deliver_control_delivery_async(
                control_interactions.terminal_delivery_for_action(
                    action,
                    f"Control action '{action.action_kind}' is not wired yet.",
                )
            )
        finally:
            with contextlib.suppress(Exception):
                await self._status_to_route_async(status_route, "working_stop", {})

    async def publish_control_catalog_async(self, *, endpoint_id: str | None = None) -> None:
        control_plane = self.context.port_registry.get("control:control")
        channel_runtime = self.context.port_registry.get("channel:channel")
        if control_plane is None or channel_runtime is None:
            return
        queue_endpoint_status = getattr(channel_runtime, "queue_endpoint_status", None)
        if not callable(queue_endpoint_status):
            return
        if endpoint_id:
            await self._deliver_control_delivery_async(control_interactions.control_catalog_delivery(control_plane, endpoint_id))
            return
        list_endpoints = getattr(channel_runtime, "list_endpoints", None)
        if not callable(list_endpoints):
            return
        for endpoint in list_endpoints():
            await self._deliver_control_delivery_async(
                control_interactions.control_catalog_delivery(control_plane, endpoint.endpoint.endpoint_id)
            )

    async def expire_pending_control_requests_async(self) -> None:
        now = datetime.now(timezone.utc)
        for scope_state in list(self.state.control_scopes.values()):
            expired: list[Any] = []
            for request_kind, request in list(scope_state.pending_requests.items()):
                if _parse_utc_timestamp(request.expires_at) <= now:
                    expired.append((request_kind, request))
                    scope_state.pending_requests.pop(request_kind, None)
            for _, request in expired:
                await self._notify_expired_request_async(request)

    def _ensure_scope_state(self, control_scope_key: str):
        scope_state = self.state.control_scopes.get(control_scope_key)
        if scope_state is None:
            from pal.core.contracts import ControlScopeState

            scope_state = ControlScopeState()
            self.state.control_scopes[control_scope_key] = scope_state
        return scope_state

    async def _handle_show_panel_async(self, action: ControlAction) -> None:
        control_plane = self.context.require_port("control:control")
        if action.route is not None:
            await self.publish_control_catalog_async(endpoint_id=action.route.endpoint_id)
        await self._deliver_control_delivery_async(control_interactions.control_panel_delivery(control_plane, action.route))

    async def _handle_show_think_async(self, action: ControlAction) -> None:
        llm_runtime = self.context.require_port("llm:llm")
        refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
        if callable(refresh):
            refresh()
        think_level = str(getattr(llm_runtime, "think_level", "balanced") or "balanced")
        await self._deliver_control_delivery_async(control_interactions.think_panel_delivery(action.route, think_level))

    async def _handle_set_think_async(self, action: ControlAction) -> None:
        requested = str(action.args.get("think_level") or "").strip() or "balanced"
        llm_runtime = self.context.require_port("llm:llm")
        settings_repository = getattr(llm_runtime, "settings_repository", None)
        if settings_repository is not None:
            settings_repository.set_think_level(requested)
        refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
        if callable(refresh):
            refresh()
        await self._complete_action_reply_async(
            action,
            f"Think level updated to {requested}. This applies to new turns only.",
        )

    async def _handle_refresh_llm_endpoint_async(self, action: ControlAction) -> None:
        llm_runtime = self.context.require_port("llm:llm")
        refresh = getattr(llm_runtime, "refresh_llm_endpoints", None)
        if callable(refresh):
            payload = refresh()
        else:
            settings_refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
            if callable(settings_refresh):
                settings_refresh()
            payload = {"enabled_count": None, "primary_endpoint_id": getattr(llm_runtime, "active_endpoint_id", None)}
        enabled_count = payload.get("enabled_count")
        primary = payload.get("primary_endpoint_id") or "-"
        active = payload.get("active_endpoint_id") or "-"
        configured = payload.get("configured_active_endpoint_id") or "-"
        added = list(payload.get("added_endpoint_ids") or [])
        removed = list(payload.get("removed_endpoint_ids") or [])
        lines = [
            "LLM endpoints refreshed.",
            f"Enabled endpoints: {enabled_count if enabled_count is not None else '-'}",
            f"Primary endpoint for future turns: {primary}",
            f"Active endpoint setting: {configured}",
        ]
        if configured != active:
            lines.append(f"Runtime active endpoint: {active}")
        if added:
            lines.append(f"Added: {', '.join(str(item) for item in added)}")
        if removed:
            lines.append(f"Removed/disabled: {', '.join(str(item) for item in removed)}")
        await self._complete_action_reply_async(action, "\n".join(lines))

    async def _handle_refresh_tool_surface_async(self, action: ControlAction) -> None:
        refresh = getattr(self.tool_surface, "reload_config", None)
        if not callable(refresh):
            await self._complete_action_reply_async(action, "Tool surface refresh is unavailable.")
            return
        try:
            payload = refresh()
        except Exception as exc:
            await self._complete_action_reply_async(action, f"Tool surface refresh failed: {exc}")
            return

        resident_names = [llm_tool_name(item) for item in list(payload.get("resident_tool_names") or []) if str(item).strip()]
        preview = ", ".join(resident_names[:12]) if resident_names else "-"
        if len(resident_names) > 12:
            preview = f"{preview}, ..."
        lines = [
            "Tool surface refreshed.",
            f"Resident tools for future turns: {payload.get('resident_tool_count', len(resident_names))}",
            f"Singleton config entries: {payload.get('singleton_count', '-')}",
            f"Dynamic config entries: {payload.get('dynamic_count', '-')}",
            f"Tools: {preview}",
        ]
        await self._complete_action_reply_async(action, "\n".join(lines))

    def artifact_scope_for_turn(self, turn_id: str | None) -> str | None:
        normalized = str(turn_id or "").strip()
        if not normalized:
            return None
        continuation = self.state.active_turns.get(normalized)
        if isinstance(continuation, TurnContinuation):
            return continuation.control_scope_key
        return self.state.turn_scopes.get(normalized)

    async def _prepare_channel_artifacts_async(
        self,
        channel_envelope: ChannelEnvelope,
        control_scope_key: str,
    ) -> ChannelEnvelope:
        payload = channel_envelope.event.payload
        if not isinstance(payload, dict):
            return channel_envelope
        attachments = payload.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return channel_envelope
        artifact_manager = self.context.port_registry.get("artifact:artifact")
        register_ingested = getattr(artifact_manager, "register_ingested", None)
        if not callable(register_ingested):
            return channel_envelope
        refs: list[dict[str, Any]] = []
        for item in attachments:
            try:
                ref = register_ingested(
                    item,
                    scope_key=control_scope_key,
                    turn_id=channel_envelope.event.event_id,
                    source_channel=channel_envelope.endpoint.channel_kind,
                    metadata={
                        "source_text": str(payload.get("text") or ""),
                        "caption": str(payload.get("caption") or payload.get("text") or ""),
                        "endpoint_id": channel_envelope.endpoint.endpoint_id,
                    },
                )
            except Exception as exc:
                self.state.diagnostics.append(
                    {
                        "kind": "artifact.register.failed",
                        "turn_id": channel_envelope.event.event_id,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                continue
            refs.append(ref.to_dict() if hasattr(ref, "to_dict") else dict(ref))
        if not refs:
            return channel_envelope
        new_payload = dict(payload)
        new_payload.pop("attachments", None)
        new_payload["artifact_refs"] = refs
        return ChannelEnvelope(
            event=EventEnvelope(
                event_kind=channel_envelope.event.event_kind,
                source_kind=channel_envelope.event.source_kind,
                payload=new_payload,
                correlation_id=channel_envelope.event.correlation_id,
                created_at=channel_envelope.event.created_at,
                event_id=channel_envelope.event.event_id,
            ),
            endpoint=channel_envelope.endpoint,
            response_handle=channel_envelope.response_handle,
        )

    async def _handle_show_log_async(self, action: ControlAction) -> None:
        await self._deliver_control_delivery_async(
            control_interactions.log_panel_delivery(action.route, bool(self.state.prompt_log_enabled))
        )

    async def _handle_set_log_async(self, action: ControlAction) -> None:
        enabled = bool(action.args.get("prompt_log_enabled"))
        self.state.prompt_log_enabled = enabled
        message = (
            "Prompt debug logging enabled for new turns."
            if enabled
            else "Prompt debug logging disabled for new turns."
        )
        await self._complete_action_reply_async(action, message)

    async def _handle_interrupt_turn_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        interrupted = await self.turn_manager.interrupt_by_scope(route.control_scope_key, reason="interrupted")
        message = "Interrupted the current turn." if interrupted else "No active turn to interrupt in this scope."
        await self._complete_action_reply_async(action, message)

    async def _handle_open_reset_confirm_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        from pal.core.contracts import PendingControlRequest

        scope_state = self._ensure_scope_state(route.control_scope_key)
        existing = scope_state.pending_requests.get("reset_confirm")
        if existing is not None:
            existing.expires_at = _utc_after_seconds(60)
            await self._render_reset_prompt_async(existing)
            return
        request = PendingControlRequest(
            request_id=f"reset_{uuid4().hex[:12]}",
            request_kind="reset_confirm",
            control_scope_key=route.control_scope_key,
            route=route,
            expires_at=_utc_after_seconds(60),
            payload={},
        )
        scope_state.pending_requests["reset_confirm"] = request
        await self._render_reset_prompt_async(request)

    async def _handle_cancel_reset_confirm_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        request_id = str(action.args.get("request_id") or action.args.get("interaction_id") or "").strip()
        scope_state = self._ensure_scope_state(route.control_scope_key)
        request = scope_state.pending_requests.get("reset_confirm")
        if request is not None and request.request_id == request_id:
            scope_state.pending_requests.pop("reset_confirm", None)
        await self._complete_action_reply_async(action, "Reset cancelled.")

    async def _handle_reset_memory_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        request_id = str(action.args.get("request_id") or "").strip()
        scope_state = self._ensure_scope_state(route.control_scope_key)
        request = scope_state.pending_requests.get("reset_confirm")
        if request is None or request.request_id != request_id:
            await self._complete_action_reply_async(action, "Reset request is missing, expired, or already consumed.")
            return
        if _parse_utc_timestamp(request.expires_at) <= datetime.now(timezone.utc):
            scope_state.pending_requests.pop("reset_confirm", None)
            await self._notify_expired_request_async(request)
            return
        await self._execute_soft_reset_async(scope_state, request)
        scope_state.pending_requests.pop("reset_confirm", None)
        await self._complete_action_reply_async(
            action,
            "Soft reset complete. L1/L2 and working memory projection were cleared.",
        )

    async def _generate_compaction_summary_async(self, source_text: str) -> str:
        llm_runtime = self.context.require_port("llm:llm")
        generate = getattr(llm_runtime, "agenerate", None)
        if not callable(generate):
            generate = getattr(llm_runtime, "generate", None)
            if not callable(generate):
                return ""
        request = CanonicalLLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize the recent conversation into a short, durable working-memory summary. "
                        "Preserve user preferences, commitments, active goals, and factual context. "
                        "Do not include markdown, speaker labels, or commentary."
                    ),
                },
                {"role": "user", "content": source_text.strip()},
            ],
            max_output_tokens=1024,
            temperature=0.2,
            tools=[],
            metadata={"response_mode_hint": "operational", "purpose": "manual_compaction"},
        )
        try:
            outcome = generate(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return str(outcome.text or "").strip()
        except Exception:
            return ""

    async def _execute_soft_reset_async(self, scope_state, request) -> None:
        async with scope_state.transition_lock:
            if scope_state.quiescing:
                return
            scope_state.quiescing = True
            scope_state.drained_event = asyncio.Event()
            scope_state.pending_channel_turns.clear()
            current_turn_id = self.turn_manager.latest_active_turn_id(request.control_scope_key)
            if current_turn_id is None:
                scope_state.drained_event.set()
        if current_turn_id is not None:
            await self.turn_manager.interrupt_by_scope(request.control_scope_key, reason="reset")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(scope_state.drained_event.wait(), timeout=2.0)
        memory_service = self.context.require_port("memory:memory")
        soft_reset = getattr(memory_service, "asoft_reset", None)
        if callable(soft_reset):
            await soft_reset()
        else:
            sync_reset = getattr(memory_service, "soft_reset", None)
            if callable(sync_reset):
                await asyncio.to_thread(sync_reset)
        async with scope_state.transition_lock:
            scope_state.quiescing = False
            scope_state.drained_event.set()

    async def _handle_compact_memory_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        memory_service = self.context.require_port("memory:memory")
        builder = getattr(memory_service, "build_compaction_source_text", None)
        if not callable(builder):
            await self._complete_compact_reply_async(action, "Memory service does not support compaction.")
            return
        source_text = str(builder(target_input_budget=8192) or "").strip()
        if not source_text:
            await self._complete_compact_reply_async(action, "Nothing to compact - memory is already minimal.")
            return
        metadata = await self.turn_executor.build_compaction_metadata_async(
            memory_service,
            target_input_budget=8192,
            reserved_output_tokens=4096,
        )
        if not metadata:
            await self._complete_compact_reply_async(action, "Compaction failed - could not generate structured summary.")
            return
        compact_method = getattr(memory_service, "acompact", None)
        if not callable(compact_method):
            compact_method = getattr(memory_service, "compact", None)
            if not callable(compact_method):
                await self._complete_compact_reply_async(action, "Compaction failed - compact method not available.")
                return
        from pal.memory.contracts import MemoryCompactRequest

        request = MemoryCompactRequest(
            target_input_budget=4096,
            reserved_output_tokens=4096,
            metadata=metadata,
        )
        try:
            result = compact_method(request)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await self._complete_compact_reply_async(
                action,
                f"Compaction failed - memory state was left unchanged. {type(exc).__name__}: {exc}",
            )
            return
        entry_count = getattr(result, "metadata", {}).get("projected_entry_count", 0) if result else 0
        summary_count = getattr(result, "metadata", {}).get("compact_summary_count", 0) if result else 0
        retired = getattr(result, "metadata", {}).get("retired_count", 0) if result else 0
        storage_text = "L1 compact summary updated." if summary_count else "No compact summary was stored."
        await self._complete_compact_reply_async(
            action,
            f"Context compacted. {storage_text} {entry_count} L2 entries projected, {retired} retired to L3.",
        )
        memory_candidates = _memory_candidates_from_compact_result(result)
        if memory_candidates:
            source_ref = f"compact_{uuid4().hex[:12]}"
            delivery = control_interactions.memory_candidate_approval_delivery(
                {
                    "source_kind": "pal_compact",
                    "source_ref": source_ref,
                    "source_label": "Pal compact",
                    "candidate_batch_id": source_ref,
                    "memory_candidates": memory_candidates,
                },
                action.route,
            )
            if delivery is not None:
                await self._deliver_control_delivery_async(delivery, fallback_route=action.route)

    async def _handle_memory_candidate_decision_async(self, action: ControlAction) -> None:
        decision = str(action.args.get("decision") or "").strip().lower()
        if decision == "reject":
            await self._complete_action_reply_async(action, "Memory candidates discarded.")
            return
        if decision == "edit":
            await self._complete_action_reply_async(
                action,
                "Memory candidate absorption paused. Edit and resubmit the candidates when ready.",
            )
            return
        if decision != "accept":
            await self._complete_action_reply_async(action, "Unknown memory candidate decision.")
            return
        memory_candidates = _dict_list(action.args.get("memory_candidates"))
        if not memory_candidates:
            await self._complete_action_reply_async(action, "No memory candidates to commit.")
            return
        runtime = getattr(self.context, "execution_runtime", None)
        if runtime is None or "op_memory_write" not in getattr(runtime, "capabilities", {}):
            await self._complete_action_reply_async(
                action,
                f"Memory candidates accepted ({len(memory_candidates)} reviewed; 0 committed; memory write unavailable).",
            )
            return
        source_kind = str(action.args.get("source_kind") or "").strip()
        source_ref = str(action.args.get("source_ref") or action.target_id or "").strip()
        default_scope = "task" if source_kind == "minion" else "system"
        fallback_task_id = source_ref if default_scope == "task" else ""
        committed = 0
        skipped = 0
        for candidate in memory_candidates:
            args = l3_commit_args_from_memory_candidate(
                candidate,
                default_scope=default_scope,
                fallback_task_id=fallback_task_id,
                source_kind=source_kind,
                source_ref=source_ref,
            )
            if not args:
                skipped += 1
                continue
            result = await runtime.execute_async(CapabilityCall(name="op_memory_write", args=args))
            if str(getattr(result, "status", "") or "") == RuntimeStatus.OK:
                committed += 1
            else:
                skipped += 1
        suffix = f"; {skipped} skipped" if skipped else ""
        await self._complete_action_reply_async(
            action,
            f"Memory candidates accepted ({len(memory_candidates)} reviewed; {committed} committed{suffix}).",
        )

    async def _handle_route_reply_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        text = str(action.args.get("text") or action.args.get("message") or action.notes or "").strip()
        if text:
            await self._deliver_control_delivery_async(control_interactions.delivery_for_reply(route, text))

    async def _handle_invoke_capability_async(self, action: ControlAction) -> None:
        capability_name = str(action.target_id or "").strip()
        if not capability_name:
            await self._reply_to_route_async(action.route, "Missing capability target.")
            return
        result = await self.context.execution_runtime.execute_async(
            CapabilityCall(name=capability_name, args=dict(action.args))
        )
        text = str(result.text or result.llm_text)
        if control_interactions.is_interaction_action(action):
            text = text[:240].strip() or text
        await self._complete_action_reply_async(action, text)

    async def _render_reset_prompt_async(self, request) -> None:
        already_opened = bool(request.payload.get("opened"))
        request.payload["opened"] = True
        await self._deliver_control_delivery_async(
            control_interactions.reset_confirm_delivery(request, already_opened=already_opened)
        )

    async def _notify_expired_request_async(self, request) -> None:
        await self._deliver_control_delivery_async(
            control_interactions.terminal_delivery_for_interaction(
                request.route,
                interaction_id=request.request_id,
                interaction_kind="reset_confirm",
                text="This reset request expired.",
                delivery_kind="interactive_expire",
            )
        )

    async def _complete_compact_reply_async(self, action: ControlAction, text: str) -> None:
        await self._complete_action_reply_async(action, text)

    async def _complete_action_reply_async(self, action: ControlAction, text: str) -> None:
        await self._deliver_control_delivery_async(control_interactions.terminal_delivery_for_action(action, text))

    async def _deliver_control_delivery_async(
        self,
        delivery: ControlDelivery,
        *,
        fallback_route: ControlRoute | None = None,
    ) -> None:
        route = delivery.route or fallback_route
        if delivery.delivery_kind == "reply":
            text = delivery.text or str(delivery.payload.get("text") or "")
            if text:
                await self._reply_to_route_async(route, text)
            return
        if delivery.delivery_kind == "endpoint_status":
            channel_runtime = self.context.port_registry.get("channel:channel")
            queue_endpoint_status = getattr(channel_runtime, "queue_endpoint_status", None)
            if not callable(queue_endpoint_status):
                return
            endpoint_id = delivery.endpoint_id or (route.endpoint_id if route is not None else "")
            status_kind = str(delivery.payload.get("status_kind") or "").strip()
            status_payload = delivery.payload.get("payload")
            if not isinstance(status_payload, dict):
                status_payload = {
                    key: value
                    for key, value in delivery.payload.items()
                    if key not in {"status_kind", "payload"}
                }
            if endpoint_id and status_kind:
                queue_endpoint_status(endpoint_id, status_kind, payload=dict(status_payload))
            return
        if delivery.delivery_kind in control_interactions.INTERACTIVE_DELIVERY_KINDS:
            if delivery.interaction is None:
                if delivery.text:
                    await self._reply_to_route_async(route, delivery.text)
                return
            payload = dict(delivery.payload)
            payload["spec"] = delivery.interaction
            if delivery.text:
                payload["text"] = delivery.text
            await self._status_to_route_async(route, delivery.delivery_kind, payload)

    async def _reply_to_route_async(self, route: ControlRoute | None, text: str) -> None:
        envelope = self._route_to_channel_envelope(route)
        if envelope is None:
            return
        channel_runtime = self.context.require_port("channel:channel")
        channel_runtime.queue_reply(envelope, text)

    async def _status_to_route_async(self, route: ControlRoute | None, kind: str, payload: dict[str, Any]) -> None:
        envelope = self._route_to_channel_envelope(route)
        if envelope is None:
            return
        channel_runtime = self.context.require_port("channel:channel")
        channel_runtime.queue_status(envelope, kind, payload=payload)

    def _route_from_channel_envelope(self, channel_envelope: ChannelEnvelope) -> ControlRoute:
        return route_from_channel_envelope(channel_envelope)

    def _route_to_channel_envelope(self, route: ControlRoute | None) -> ChannelEnvelope | None:
        if route is None:
            return None
        channel_runtime = self.context.port_registry.get("channel:channel")
        endpoint = channel_runtime.get_endpoint(route.endpoint_id) if channel_runtime is not None else None
        endpoint_config = endpoint.endpoint if endpoint is not None else None
        if endpoint_config is None:
            from pal.shared import EndpointConfig, ResponseHandle

            endpoint_config = EndpointConfig(
                endpoint_id=route.endpoint_id,
                channel_kind=route.channel_kind,
                binding_key=route.control_scope_key,
            )
            response_handle = ResponseHandle(endpoint_id=route.endpoint_id, reply_target=dict(route.reply_target))
        else:
            response_handle = endpoint.build_response_handle(reply_target=dict(route.reply_target))
        return ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.CONTROL_ACTION,
                source_kind=SourceKind.CONTROL,
                payload={},
                correlation_id=route.correlation_id,
            ),
            endpoint=endpoint_config,
            response_handle=response_handle,
        )

    async def _run_turn_continuation_async(self, continuation: TurnContinuation) -> TurnOutcome:
        current: EffectResult | None = None
        try:
            while True:
                if continuation.interrupted:
                    raise asyncio.CancelledError(continuation.interrupt_reason or "interrupted")
                yielded = self.turn_manager.resume(continuation, current)
                if isinstance(yielded, TurnOutcome):
                    outcome = self._enrich_transcript_with_tool_protocol(yielded, continuation)
                    await self._schedule_post_turn_commit_async(outcome)
                    return outcome
                current = await self._execute_turn_effect_async(continuation, yielded)
        except asyncio.CancelledError:
            await self.turn_manager.commit_l1_exit_checkpoint_async(
                continuation,
                kind=L1MessageKind.TURN_INTERRUPTED,
                status="interrupted",
                reason=continuation.interrupt_reason or "interrupted",
            )
            raise
        except Exception as exc:
            await self.turn_manager.commit_l1_exit_checkpoint_async(
                continuation,
                kind=L1MessageKind.TURN_ABORTED,
                status="aborted",
                reason=f"{exc.__class__.__name__}: {exc}",
            )
            raise

    def _truncate_tool_result_for_l1(self, content: str) -> str:
        max_chars = max(0, int(getattr(self.config, "l1_tool_result_max_chars", 8_000) or 0))
        if not max_chars or len(content) <= max_chars:
            return content
        configured_preview = int(getattr(self.config, "l1_tool_result_preview_chars", 4_000) or 0)
        preview_chars = max(1, min(configured_preview or max_chars, max_chars))
        preview = content[:preview_chars].rstrip()
        return f"{preview}\n\n[... truncated, original: {len(content)} chars]"

    def _enrich_transcript_with_tool_protocol(self, outcome: TurnOutcome, continuation: TurnContinuation) -> TurnOutcome:
        from pal.memory.contracts import L1MessageKind, L1TranscriptMessage

        if not continuation.tool_protocol_messages:
            return outcome
        original = outcome.commit_payload.transcript
        user_msg = next((m for m in original if m.role == "user"), None)
        new_transcript: list[L1TranscriptMessage] = []
        if user_msg:
            new_transcript.append(user_msg)
        protocol_transcript, protocol_assistant_contents = self.turn_manager._build_l1_tool_protocol_transcript(
            continuation,
            diagnostic_kind="memory.commit.tool_protocol_invalid",
        )
        new_transcript.extend(protocol_transcript)
        assistant_msgs = [m for m in original if m.role == "assistant"]
        for m in assistant_msgs:
            content = str(m.content or "").strip()
            if content and content in protocol_assistant_contents:
                protocol_assistant_contents.remove(content)
                continue
            new_transcript.append(L1TranscriptMessage(
                role=m.role,
                content=m.content,
                kind=L1MessageKind.ASSISTANT_REPLY,
            ))
        return TurnOutcome(
            turn_id=outcome.turn_id,
            final_reply=outcome.final_reply,
            commit_payload=L1CommitPayload(
                turn_id=outcome.commit_payload.turn_id,
                transcript=new_transcript,
                tool_observations=outcome.commit_payload.tool_observations,
            ),
            reply_texts=outcome.reply_texts,
        )

    async def _call_port_async(self, port, async_name: str, sync_name: str, *args, **kwargs):
        async_method = getattr(port, async_name, None)
        if callable(async_method):
            result = async_method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        sync_method = getattr(port, sync_name)
        return await asyncio.to_thread(sync_method, *args, **kwargs)

    def publish_module_capabilities(self, module_id: str) -> list[str]:
        return self.module_lifecycle.publish_module_capabilities(module_id)

    def withdraw_module_capabilities(self, module_id: str) -> list[str]:
        return self.module_lifecycle.withdraw_module_capabilities(module_id)

    def mount_module(self, handle: ModuleHandle) -> ModuleHandle:
        return self.module_lifecycle.mount_module(handle)

    def detach_module(self, module_id: str) -> str:
        return self.module_lifecycle.detach_module(module_id)

    def reattach_module(self, module_id: str) -> str:
        return self.module_lifecycle.reattach_module(module_id)

    def collect_prompt_fragments(self, assembly_context: PromptAssemblyContext) -> list[PromptFragment]:
        return self.prompt_compiler.collect_prompt_fragments(assembly_context)

    def build_prompt_ir(self, assembly_context: PromptAssemblyContext):
        return self.prompt_compiler.build_prompt_ir(assembly_context)

    def build_canonical_prompt(
        self,
        assembly_context: PromptAssemblyContext,
        *,
        max_output_tokens: int = 1024,
        model_hint: str | None = None,
    ):
        return self.prompt_compiler.build_canonical_prompt(
            assembly_context,
            max_output_tokens=max_output_tokens,
            model_hint=model_hint,
        )

    def _execute_turn_effect(self, continuation: TurnContinuation, effect: EffectRequest) -> EffectResult:
        return self.turn_executor.execute_turn_effect(continuation, effect)

    async def _execute_turn_effect_async(self, continuation: TurnContinuation, effect: EffectRequest) -> EffectResult:
        return await self.turn_executor.execute_turn_effect_async(continuation, effect)

    def _build_llm_tool_contracts(self) -> list[dict[str, object]]:
        return self.tool_surface.build_llm_tool_contracts()

    def _build_tool_contracts_from_descriptors(self, descriptors: list[Any]) -> list[dict[str, object]]:
        return self.tool_surface.build_tool_contracts_from_descriptors(descriptors)

    def _select_llm_descriptors(self) -> list:
        return self.tool_surface.select_llm_descriptors()

    def _select_failure_descriptors(self, signal: FailureSignal) -> list[Any]:
        return self.tool_surface.select_failure_descriptors(signal)

    async def handle_failure_async(
        self,
        signal: FailureSignal,
        *,
        origin: str,
        route: str | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> FailureHandlingResult:
        return await self.failure_orchestrator.handle_failure_async(
            signal,
            origin=origin,
            route=route,
            conversation_context=conversation_context,
        )

    def _render_failure_feedback_text(self, feedback: FailureUserFeedback) -> str:
        return self.failure_orchestrator.render_failure_feedback_text(feedback)

    def _should_enter_failure_flow_for_tool_result(self, tool_result) -> bool:
        if getattr(tool_result, "ok", True):
            return False
        text = str(getattr(tool_result, "text", "") or "")
        return text.startswith("tool execution failed:") or text.startswith("capability execution failed:")

    def _stream_llm_request(
        self,
        continuation: TurnContinuation,
        llm_runtime,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        return asyncio.run(self._stream_llm_request_async(continuation, llm_runtime, request))

    async def _stream_llm_request_async(
        self,
        continuation: TurnContinuation,
        llm_runtime,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        return await self.turn_executor.stream_llm_request_async(continuation, llm_runtime, request)

    def _build_turn_prompt(
        self,
        continuation: TurnContinuation,
        assembly_context: PromptAssemblyContext,
        *,
        max_output_tokens: int,
    ) -> CanonicalLLMRequest:
        return self.turn_executor.build_turn_prompt(
            continuation,
            assembly_context,
            max_output_tokens=max_output_tokens,
        )

    def _fallback_final_reply(self, continuation: TurnContinuation) -> str:
        return self.turn_executor.fallback_final_reply(continuation)

    def _should_stream_reply(self, channel_envelope: ChannelEnvelope) -> bool:
        return self.turn_executor.should_stream_reply(channel_envelope)

    def _infer_response_mode(self, outcome: CanonicalLLMOutcome | None, *, used_tools: bool) -> str:
        return self.turn_executor.infer_response_mode(outcome, used_tools=used_tools)

    def _select_turn_temperature(self, response_mode: str) -> float:
        return self.turn_executor.select_turn_temperature(response_mode)

    def _debug_log_prompt(self, *args) -> None:
        if not self._prompt_log_enabled_from_args(*args):
            return
        request = _last_arg_of_type(args, CanonicalLLMRequest)
        if request is None:
            return
        self._append_prompt_log(
            "\n".join(
                [
                    "=== PAL PROMPT DEBUG ===",
                    "--- request.messages ---",
                    str(request.messages),
                    "--- request.multimodal ---",
                    _summarize_multimodal_prompt(request.messages),
                    "--- request.tools ---",
                    str(request.tools),
                    "=== END PAL PROMPT DEBUG ===",
                ]
            )
        )

    def _debug_log_outcome(self, *args) -> None:
        if not self._prompt_log_enabled_from_args(*args):
            return
        outcome = _last_arg_of_type(args, CanonicalLLMOutcome)
        if outcome is None:
            return
        provider_payload = _summarize_last_provider_payload(self.context.port_registry.get("llm:llm"))
        self._append_prompt_log(
            "\n".join(
                [
                    "=== PAL LLM OUTCOME ===",
                    "--- provider.payload ---",
                    provider_payload,
                    f"finish_reason: {outcome.finish_reason}",
                    f"response_mode: {outcome.response_mode}",
                    f"tool_calls: {outcome.tool_calls}",
                    f"reasoning_text (first 500): {str(outcome.reasoning_text or '')[:500]}",
                    f"text (first 2000): {str(outcome.text or '')[:2000]}",
                    "=== END PAL LLM OUTCOME ===",
                ]
            )
        )

    def _debug_log_reply(self, *args) -> None:
        if not self._prompt_log_enabled_from_args(*args):
            return
        text = str(args[-1] if args else "")
        self._append_prompt_log(
            "\n".join(
                [
                    "=== PAL TG REPLY ===",
                    text,
                    "=== END PAL TG REPLY ===",
                ]
            )
        )

    def _prompt_log_enabled_from_args(self, *args) -> bool:
        for item in args:
            if isinstance(item, TurnContinuation):
                return bool(item.turn_settings_snapshot.get("prompt_log_enabled"))
        request = _last_arg_of_type(args, CanonicalLLMRequest)
        if request is not None and "prompt_log_enabled" in request.metadata:
            return bool(request.metadata.get("prompt_log_enabled"))
        return bool(self.state.prompt_log_enabled)

    def _append_prompt_log(self, text: str) -> None:
        root = getattr(self.config, "runtime_root", None)
        if root is None:
            print(text)
            return
        log_path = Path(root) / "pal.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")

    def _schedule_post_turn_commit(self, outcome: TurnOutcome) -> None:
        asyncio.run(self._schedule_post_turn_commit_async(outcome))

    async def _schedule_post_turn_commit_async(self, outcome: TurnOutcome) -> None:
        await self.turn_executor.schedule_post_turn_commit_async(outcome)

    async def _summarize_compaction_async(
        self,
        memory_service,
        *,
        target_input_budget: int,
        reserved_output_tokens: int,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> str:
        return await self.turn_executor.summarize_compaction_async(
            memory_service,
            target_input_budget=target_input_budget,
            reserved_output_tokens=reserved_output_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_model_id=preferred_model_id,
        )

    def _build_compaction_source_text(self, memory_service, *, target_input_budget: int) -> str:
        return self.turn_executor.build_compaction_source_text(memory_service, target_input_budget=target_input_budget)


def _memory_candidates_from_compact_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    for entry in list(getattr(result, "projected_entries", []) or []):
        payload = getattr(entry, "payload", None)
        if isinstance(payload, dict):
            candidates = _dict_list(payload.get("memory_candidates"))
            if candidates:
                return candidates
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        return _dict_list(metadata.get("memory_candidates"))
    return []


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def effect_result_to_observation(tool_result) -> "ToolObservation":
    from pal.core.turns import ToolObservation

    return ToolObservation(
        tool_name=tool_result.name,
        ok=tool_result.ok,
        summary=tool_result.text or ("tool succeeded" if tool_result.ok else "tool failed"),
        structured=tool_result.structured,
    )


def _utc_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _parse_utc_timestamp(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _last_arg_of_type(args: tuple[Any, ...], expected_type):
    for item in reversed(args):
        if isinstance(item, expected_type):
            return item
    return None


def _summarize_multimodal_prompt(messages: list[dict[str, Any]]) -> str:
    items: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type == "artifact_image":
                items.append(
                    {
                        "message_index": index,
                        "type": "artifact_image",
                        "artifact_id": part.get("artifact_id"),
                        "representation_id": part.get("representation_id"),
                        "mime_type": part.get("mime_type"),
                        "bytes": "omitted",
                    }
                )
            elif part_type == "image_url":
                url = str((part.get("image_url") or {}).get("url") or "")
                items.append(
                    {
                        "message_index": index,
                        "type": "image_url",
                        "url_prefix": url[:32],
                        "url_length": len(url),
                        "bytes": "omitted",
                    }
                )
    return str(items)


def _summarize_last_provider_payload(llm_runtime: Any) -> str:
    invoker = getattr(llm_runtime, "endpoint_invoker", None)
    summary = getattr(invoker, "last_payload_summary", None)
    return str(summary or {})
