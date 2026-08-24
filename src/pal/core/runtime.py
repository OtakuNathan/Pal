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
from pal.core.agent_turn_runtime import AgentTurnRuntime
from pal.core.cache_warm_deadline import (
    CacheWarmDeadlineManager,
    CacheWarmDeadlineNotice,
)
from pal.core.contracts import CoreRuntimeState
from pal.core.runtime_config import RuntimeConfig
from pal.core.dispatcher import EventDispatcher
from pal.core.failure_orchestrator import FailureHandlingResult, FailureOrchestrator
from pal.core.main_context import MainContext
from pal.core.module_lifecycle import ModuleLifecycle
from pal.core.prompt_debug_log import (
    append_prompt_debug_log,
    render_llm_outcome_debug_log,
    render_prompt_debug_log,
    render_reply_debug_log,
    summarize_last_provider_payload,
)
from pal.core.prompt_compiler import PromptCompiler
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.tool_stagnation import ToolStagnationGuardProcess
from pal.core.tool_surface import ToolSurface
from pal.core.turn_executor import TurnExecutor
from pal.core.turn_events import TURN_END, TURN_START
from pal.core.turns import EffectRequest, EffectResult, TurnContinuation, TurnOutcome, L1CommitPayload, channel_turn_program
from pal.core.module_registry import ModuleHandle
from pal.execution import CapabilityCall
from pal.execution.contracts import CapabilityResult
from pal.foundation import AttachmentSpec, EventEnvelope, utc_now
from pal.foundation.log_paths import pal_log_path
from pal.failure import FailureSignal, FailureUserFeedback
from pal.llm.contracts import LLMGenerationResult
from pal.llm.ir import LLMRequestIR
from pal.memory import L1MessageKind, L1TranscriptMessage, MemoryCommitRequest
from pal.memory.compact import coerce_memory_candidate_list, memory_candidates_from_compact_result
from pal.memory.interactions import memory_candidate_approval_delivery
from pal.memory.tool_protocol import l1_tool_protocol_transcript
from pal.shared import ChannelEnvelope, EventKind, SourceKind, TurnDeliveryBinding
from pal.shared import IntrospectionPort, PromptAssemblyContext, PromptFragment, RuntimeStatus
from pal.shared.payloads import extract_text_from_payload


@dataclass
class TurnManager:
    context: MainContext
    state: CoreRuntimeState
    guard: ToolStagnationGuardProcess = field(default_factory=ToolStagnationGuardProcess)
    config: RuntimeConfig = field(default_factory=RuntimeConfig.defaults)

    def start(
        self,
        channel_envelope: ChannelEnvelope,
        *,
        delivery_binding: TurnDeliveryBinding | None = None,
    ) -> TurnContinuation:
        turn_id = channel_envelope.event.event_id
        max_output_tokens = self._resolve_max_output_tokens()
        if delivery_binding is None:
            delivery_binding = channel_envelope.opening_delivery_binding
        if delivery_binding is None:
            control_scope_key = derive_control_scope_key(
                endpoint_id=channel_envelope.endpoint.endpoint_id,
                channel_kind=channel_envelope.endpoint.channel_kind,
                reply_target=channel_envelope.response_handle.reply_target,
                payload=channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {},
            )
            delivery_binding = TurnDeliveryBinding.from_envelope(
                channel_envelope,
                control_scope_key=control_scope_key,
            )
        control_scope_key = delivery_binding.control_scope_key
        continuation = TurnContinuation(
            turn_id=turn_id,
            opening_event=channel_envelope.event,
            delivery_binding=delivery_binding,
            program=channel_turn_program(
                channel_envelope.event,
                core_mode=self.state.mode,
                max_output_tokens=max_output_tokens,
            ),
            correlation_id=channel_envelope.event.correlation_id or turn_id,
            control_scope_key=control_scope_key,
            turn_settings_snapshot=self._build_turn_settings_snapshot(),
        )
        self.state.active_turns[turn_id] = continuation
        self.state.turn_scopes[turn_id] = control_scope_key
        self._remember_active_turn(turn_id)
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

    async def interrupt_active_turn(self, *, reason: str = "interrupted") -> bool:
        turn_id = self.latest_active_turn_id()
        if not turn_id:
            return False
        async with self.state.resident_interrupt_lock:
            if not self._is_turn_live(turn_id):
                return False
            in_flight = self.state.resident_interrupt_task
            if (
                self.state.resident_interrupting_turn_id == turn_id
                and in_flight is not None
                and not in_flight.done()
            ):
                interrupt_task = in_flight
            else:
                interrupt_task = asyncio.create_task(
                    self._interrupt_turn_async(turn_id, reason=reason)
                )
                self.state.resident_interrupting_turn_id = turn_id
                self.state.resident_interrupt_task = interrupt_task
        return await interrupt_task

    async def _interrupt_turn_async(self, turn_id: str, *, reason: str) -> bool:
        current_task = asyncio.current_task()
        try:
            continuation = self.state.active_turns.get(turn_id)
            if isinstance(continuation, TurnContinuation):
                continuation.interrupted = True
                continuation.interrupt_reason = reason
                await self._close_l1_turn_async(continuation, state="interrupted", reason=reason)
            output_port = self.context.port_registry.get("agent_io:output") or self.context.port_registry.get("channel:channel")
            if (
                output_port is not None
                and isinstance(continuation, TurnContinuation)
                and continuation.delivery_binding is not None
            ):
                abort_result = output_port.abort_stream(continuation.delivery_binding.response_handle, reason=reason)
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
            async with self.state.resident_interrupt_lock:
                if self.state.resident_interrupt_task is current_task:
                    self.state.resident_interrupt_task = None
                    if self.state.resident_interrupting_turn_id == turn_id:
                        self.state.resident_interrupting_turn_id = None

    def cleanup_interrupted(self, turn_id: str, *, reason: str = "interrupted") -> None:
        continuation = self.state.active_turns.pop(turn_id, None)
        if isinstance(continuation, TurnContinuation):
            continuation.interrupted = True
            continuation.interrupt_reason = reason
            memory_service = self.context.port_registry.get("memory:memory")
            close = getattr(memory_service, "interrupt_l1_turn", None)
            if callable(close):
                try:
                    close(
                        continuation.turn_id,
                        reason=reason,
                    )
                except Exception:
                    pass
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
        _ = (kind, status)
        await self._close_l1_turn_async(continuation, state="aborted", reason=reason)

    async def _close_l1_turn_async(
        self,
        continuation: TurnContinuation,
        *,
        state: str,
        reason: str,
    ) -> None:
        memory_service = self.context.port_registry.get("memory:memory")
        method_name = {
            "interrupted": "interrupt_l1_turn",
            "aborted": "abort_l1_turn",
        }.get(state)
        method = getattr(memory_service, method_name or "", None)
        if not callable(method):
            return
        try:
            value = method(
                continuation.turn_id,
                reason=reason,
            )
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            self.state.diagnostics.append(
                {
                    "kind": f"memory.turn.{state}_failed",
                    "turn_id": continuation.turn_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    def _build_turn_settings_snapshot(self) -> dict[str, Any]:
        llm_runtime = self.context.port_registry.get("llm:llm")
        think_levels: dict[str, str] = {}
        if llm_runtime is not None:
            refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
            snapshot = getattr(llm_runtime, "thinking_levels_snapshot", None)
            if callable(snapshot):
                try:
                    think_levels = dict(snapshot())
                except Exception:
                    think_levels = {}
        return {
            "think_levels": think_levels,
            "prompt_log_enabled": bool(self.state.prompt_log_enabled),
        }

    def latest_active_turn_id(self) -> str | None:
        turn_id = self.state.active_turn_id
        if turn_id and self._is_turn_live(turn_id):
            self.state.resident_drained_event.clear()
            return turn_id
        self.state.active_turn_id = None
        self.state.resident_drained_event.set()
        return None

    def _remember_active_turn(self, turn_id: str) -> None:
        self.state.active_turn_id = turn_id
        self.state.resident_drained_event.clear()

    def _is_turn_live(self, turn_id: str) -> bool:
        task = self.state.turn_tasks.get(turn_id)
        if task is not None and not task.done():
            return True
        return turn_id in self.state.active_turns

    def _mark_turn_exited(self, turn_id: str) -> None:
        if self.state.active_turn_id == turn_id:
            self.state.active_turn_id = None
            self.state.resident_drained_event.set()
        self.state.turn_scopes.pop(turn_id, None)


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

    def capture_delivery_binding(self, turn_id: str | None) -> dict[str, Any]:
        return self.core.capture_delivery_binding(turn_id)


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
    agent_turn_runtime: AgentTurnRuntime = field(init=False)
    turn_executor: TurnExecutor = field(init=False)
    module_lifecycle: ModuleLifecycle = field(init=False)
    cache_warm_deadline: CacheWarmDeadlineManager = field(init=False)

    def __post_init__(self) -> None:
        self.turn_manager = TurnManager(
            context=self.context,
            state=self.state,
            guard=ToolStagnationGuardProcess.from_config(self.config),
            config=self.config,
        )
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
        self.agent_turn_runtime = AgentTurnRuntime.build(
            context=self.context,
            config=self.config,
            call_port_async=self._call_port_async,
            debug_log_prompt=self._debug_log_prompt,
            debug_log_outcome=self._debug_log_outcome,
            debug_log_reply=self._debug_log_reply,
            build_llm_tool_contracts=self._build_llm_tool_contracts,
            handle_failure_async=self.handle_failure_async,
            render_failure_feedback_text=self._render_failure_feedback_text,
            should_enter_failure_flow_for_tool_result=self._should_enter_failure_flow_for_tool_result,
            state=self.state,
            guard_host=self.turn_manager,
            compaction_policy=PalCompactionPolicy(),
            compaction_clock_provider=lambda: self.state.compaction_user_turn_count,
            after_tool_batch=self._after_tool_batch_async,
        )
        self.prompt_compiler = self.agent_turn_runtime.prompt_compiler
        self.turn_executor = self.agent_turn_runtime.executor
        self.context.execution_runtime.register_provider_ref("core:turn_io", CoreTurnIOPort(core=self))
        self.cache_warm_deadline = CacheWarmDeadlineManager(
            cache_snapshot=self._prompt_cache_warm_deadline_snapshot,
            settings_provider=self._runtime_settings_repository,
            has_active_turn=lambda: bool(
                self.state.active_turn_id or self.state.active_turns
            ),
            deliver_notice=self._deliver_cache_warm_deadline_notice_async,
            expire_notice=self._expire_cache_warm_deadline_notice_async,
        )

    def close(self) -> None:
        self.cache_warm_deadline.close()

    def _runtime_settings_repository(self) -> Any | None:
        llm_runtime = self.context.port_registry.get("llm:llm")
        return getattr(llm_runtime, "settings_repository", None)

    def _prompt_cache_warm_deadline_snapshot(self) -> dict[str, Any]:
        llm_runtime = self.context.port_registry.get("llm:llm")
        snapshot = getattr(
            llm_runtime,
            "prompt_cache_warm_deadline_snapshot",
            None,
        )
        return dict(snapshot() or {}) if callable(snapshot) else {}

    async def _deliver_cache_warm_deadline_notice_async(
        self,
        notice: CacheWarmDeadlineNotice,
    ) -> bool:
        delivery = control_interactions.cache_warm_deadline_delivery(
            notice.route,
            epoch=notice.epoch,
            prefix_tokens=notice.prefix_tokens,
            lead_seconds=notice.lead_seconds,
            expires_at=notice.expires_at,
        )
        return await self._deliver_control_delivery_async(
            delivery,
            require_provider=True,
        )

    async def _expire_cache_warm_deadline_notice_async(
        self,
        notice: CacheWarmDeadlineNotice,
    ) -> bool:
        delivery = control_interactions.cache_warm_deadline_expire_delivery(
            notice.route,
            epoch=notice.epoch,
        )
        return await self._deliver_control_delivery_async(
            delivery,
            require_provider=True,
        )

    async def _after_tool_batch_async(self, continuation: TurnContinuation) -> None:
        from pal.core.interjection import inject_pending_interjection_async

        await inject_pending_interjection_async(
            context=self.context,
            state=self.state,
            continuation=continuation,
        )

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
        if continuation.delivery_binding is None:
            return CapabilityResult(
                status=RuntimeStatus.UNSUPPORTED,
                text="active turn has no delivery authority",
                llm_text="Could not send attachment: active turn has no delivery authority.",
                structured={
                    "reason": "delivery_authority_missing",
                    "turn_id": normalized_turn_id,
                },
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
        attachment_id = queue_attachment(continuation.delivery_binding, normalized)
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
        opening_binding = channel_envelope.opening_delivery_binding
        if opening_binding is not None:
            return opening_binding.control_scope_key
        return derive_control_scope_key(
            endpoint_id=channel_envelope.endpoint.endpoint_id,
            channel_kind=channel_envelope.endpoint.channel_kind,
            reply_target=channel_envelope.response_handle.reply_target,
            payload=channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {},
        )

    def _turn_task_running(self, turn_id: str) -> bool:
        task = self.state.turn_tasks.get(turn_id)
        return task is not None and not task.done()

    def _channel_turn_is_pending(self, turn_id: str) -> bool:
        return any(
            getattr(getattr(envelope, "event", None), "event_id", None) == turn_id
            for envelope in self.state.pending_channel_turns
        )

    def _queue_channel_status(self, channel_envelope: ChannelEnvelope, kind: str, payload: dict[str, Any] | None = None) -> None:
        channel_runtime = self.context.port_registry.get("channel:channel")
        if channel_runtime is not None:
            channel_runtime.queue_status(
                self._delivery_binding_for_envelope(channel_envelope),
                kind,
                payload=dict(payload or {}),
            )

    def _delivery_binding_for_envelope(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> TurnDeliveryBinding:
        binding = channel_envelope.opening_delivery_binding
        if binding is not None:
            return binding
        return TurnDeliveryBinding.from_envelope(
            channel_envelope,
            control_scope_key=self._derive_channel_control_scope_key(channel_envelope),
        )

    def _start_channel_turn_task_locked(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> asyncio.Task[Any]:
        delivery_binding = channel_envelope.opening_delivery_binding
        if delivery_binding is None:
            control_scope_key = self._derive_channel_control_scope_key(channel_envelope)
            delivery_binding = TurnDeliveryBinding.from_envelope(
                channel_envelope,
                control_scope_key=control_scope_key,
            )
        control_scope_key = delivery_binding.control_scope_key
        turn_id = channel_envelope.event.event_id
        self.context.turn_event_bus.emit(TURN_START, {
            "turn_id": turn_id,
            "scope_key": control_scope_key,
            "endpoint_id": channel_envelope.endpoint.endpoint_id,
            "channel_kind": channel_envelope.endpoint.channel_kind,
            "reply_target": dict(channel_envelope.response_handle.reply_target),
        })
        self.turn_manager._remember_active_turn(turn_id)
        self.state.turn_scopes[turn_id] = control_scope_key
        task = asyncio.create_task(self._background_channel_turn_runner_async(channel_envelope))
        self.state.turn_tasks[turn_id] = task
        task.add_done_callback(lambda finished, current_turn_id=turn_id: self._on_turn_task_done(current_turn_id, finished))
        return task

    async def _start_next_queued_turn_async(self) -> None:
        async with self.state.channel_turn_transition_lock:
            if self.state.resident_quiescing:
                return
            if self.turn_manager.latest_active_turn_id() is not None:
                return
            while self.state.pending_channel_turns:
                pending = self.state.pending_channel_turns.popleft()
                next_envelope = await self._prepare_channel_turn_async(pending)
                next_turn_id = next_envelope.event.event_id
                if self._turn_task_running(next_turn_id):
                    continue
                self._start_channel_turn_task_locked(next_envelope)
                return

    async def schedule_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> None:
        # Any new user activity makes the old idle-cache deadline irrelevant,
        # even if this turn must queue behind the current one.
        await self.cache_warm_deadline.clear_for_user_activity()
        channel_envelope = await self._prepare_channel_turn_async(channel_envelope)
        turn_id = channel_envelope.event.event_id
        async with self.state.channel_turn_transition_lock:
            if self._turn_task_running(turn_id) or self._channel_turn_is_pending(turn_id):
                # Duplicate arrival for a turn that is already running or
                # already queued: nothing to do. The active turn's
                # working/typing status must keep running.
                return
            if self.state.resident_quiescing or self.turn_manager.latest_active_turn_id() is not None:
                # Busy: queue the envelope. The active turn keeps its typing
                # status; working_stop is only emitted when that turn actually
                # ends (turn.end / runner finally). An interjection may later
                # be injected from this queue without ever starting its own
                # turn, so stopping typing here would leave the chat silently
                # idle while the tool chain is still running.
                self.state.pending_channel_turns.append(channel_envelope)
                return
            self._start_channel_turn_task_locked(channel_envelope)

    def process_channel_turn(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        return asyncio.run(self.process_channel_turn_async(channel_envelope))

    async def process_channel_turn_async(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> TurnOutcome:
        if self.state.resident_quiescing:
            raise RuntimeError("resident runtime is quiescing")
        await self.cache_warm_deadline.clear_for_user_activity()
        channel_envelope = await self._prepare_channel_turn_async(channel_envelope)
        # The hot path is: start turn -> interpret yielded effects -> resume
        # until the generator returns a TurnOutcome.
        continuation = self.turn_manager.start(
            channel_envelope,
            delivery_binding=channel_envelope.opening_delivery_binding,
        )
        self._begin_tool_result_turn(continuation)
        return await self._run_turn_continuation_async(continuation)

    def turn_execution_options(self) -> dict[str, Any]:
        return {
            "core_mode": self.state.mode,
            "max_output_tokens": self.turn_manager._resolve_max_output_tokens(),
        }

    def track_turn_task(self, continuation: TurnContinuation, task: asyncio.Task[Any]) -> None:
        binding = continuation.delivery_binding
        if binding is not None:
            if not continuation.control_scope_key:
                continuation.control_scope_key = binding.control_scope_key
            self.state.turn_scopes[continuation.turn_id] = continuation.control_scope_key
        if not continuation.turn_settings_snapshot:
            continuation.turn_settings_snapshot = self.turn_manager._build_turn_settings_snapshot()
        self.state.active_turns[continuation.turn_id] = continuation
        self.state.turn_tasks[continuation.turn_id] = task
        self.context.turn_event_bus.emit(
            TURN_START,
            self._tracked_turn_event_payload(continuation),
        )
        task.add_done_callback(
            lambda finished, current_continuation=continuation: self._on_tracked_turn_task_done(
                current_continuation,
                finished,
            )
        )

    @staticmethod
    def _tracked_turn_event_payload(continuation: TurnContinuation) -> dict[str, Any]:
        binding = continuation.delivery_binding
        opening_event = continuation.opening_event
        return {
            "turn_id": continuation.turn_id,
            "scope_key": (
                continuation.control_scope_key
                or (binding.control_scope_key if binding is not None else "")
            ),
            "endpoint_id": binding.endpoint.endpoint_id if binding is not None else "",
            "channel_kind": binding.endpoint.channel_kind if binding is not None else "",
            "reply_target": (
                dict(binding.response_handle.reply_target)
                if binding is not None
                else {}
            ),
            "event_kind": str(getattr(opening_event, "event_kind", "") or ""),
            "source_kind": str(getattr(opening_event, "source_kind", "") or ""),
        }

    def _on_tracked_turn_task_done(
        self,
        continuation: TurnContinuation,
        task: asyncio.Task[Any],
    ) -> None:
        status = "success"
        if task.cancelled():
            status = "interrupted"
        else:
            try:
                result = task.result()
            except asyncio.CancelledError:
                status = "interrupted"
            except Exception:
                status = "failed"
            else:
                reported_status = str(result or "").strip().lower()
                if reported_status in {"success", "failed", "interrupted"}:
                    status = reported_status
        if continuation.turn_id in self.state.active_turns:
            if status == "success":
                status = "failed"
            self.turn_manager.cleanup_interrupted(
                continuation.turn_id,
                reason=status,
            )
        self.context.turn_event_bus.emit(
            TURN_END,
            {
                **self._tracked_turn_event_payload(continuation),
                "status": status,
            },
        )
        self._on_turn_task_done(continuation.turn_id, task)

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
            scope_key=self.state.resident_execution_lifetime_id,
            input_id=continuation.turn_id,
            retention_user_turns=getattr(self.config, "tool_result_pager_retention_user_turns", 5),
        )

    async def _background_channel_turn_runner_async(self, channel_envelope: ChannelEnvelope) -> None:
        turn_id = channel_envelope.event.event_id
        delivery_binding = channel_envelope.opening_delivery_binding
        control_scope_key = (
            delivery_binding.control_scope_key
            if delivery_binding is not None
            else self._derive_channel_control_scope_key(channel_envelope)
        )
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
            self.context.turn_event_bus.emit(TURN_END, {
                "turn_id": turn_id,
                "scope_key": control_scope_key,
                "endpoint_id": channel_envelope.endpoint.endpoint_id,
                "channel_kind": channel_envelope.endpoint.channel_kind,
                "reply_target": dict(channel_envelope.response_handle.reply_target),
                "status": turn_status,
            })
            self._queue_channel_status(channel_envelope, "working_stop")
            await self._start_next_queued_turn_async()

    def _on_turn_task_done(self, turn_id: str, task: asyncio.Task[Any]) -> None:
        stored = self.state.turn_tasks.get(turn_id)
        if stored is task:
            self.state.turn_tasks.pop(turn_id, None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        self.notify_ready()

    async def handle_control_action_async(
        self,
        action: ControlAction,
        *,
        require_provider: bool = False,
    ) -> bool | None:
        await self.expire_pending_control_requests_async()
        status_route = action.route or (action.delivery.route if action.delivery is not None else None)
        with contextlib.suppress(Exception):
            await self._status_to_route_async(status_route, "typing_start", {})
        try:
            if action.delivery is not None:
                return await self._deliver_control_delivery_async(
                    action.delivery,
                    fallback_route=action.route,
                    require_provider=require_provider,
                )
            if action.action_kind == "show_panel":
                await self._handle_show_panel_async(action)
                return
            if action.action_kind == "show_think":
                await self._handle_show_think_async(action)
                return
            if action.action_kind == "set_think":
                await self._handle_set_think_async(action)
                return
            if action.action_kind == "show_model":
                await self._handle_show_model_async(action)
                return
            if action.action_kind == "set_model":
                await self._handle_set_model_async(action)
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
            if action.action_kind == "refresh_llm_endpoint":
                await self._handle_refresh_llm_endpoint_async(action)
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
        status_builder = getattr(llm_runtime, "thinking_status", None)
        think_status = status_builder() if callable(status_builder) else {
            "available": False,
            "endpoint_id": getattr(llm_runtime, "active_endpoint_id", None),
            "current": None,
            "choices": [],
        }
        await self._deliver_control_delivery_async(
            control_interactions.think_panel_delivery(action.route, think_status)
        )

    async def _handle_set_think_async(self, action: ControlAction) -> None:
        requested = str(action.args.get("think_level") or "").strip()
        llm_runtime = self.context.require_port("llm:llm")
        setter = getattr(llm_runtime, "set_think_level", None)
        if not callable(setter):
            await self._complete_action_reply_async(action, "Think-level configuration is unavailable.")
            return
        try:
            resolved = setter(requested)
        except ValueError as exc:
            await self._complete_action_reply_async(action, str(exc))
            return
        status_builder = getattr(llm_runtime, "thinking_status", None)
        status = status_builder() if callable(status_builder) else {}
        endpoint_id = str(status.get("endpoint_id") or getattr(llm_runtime, "active_endpoint_id", "") or "")
        await self._complete_action_reply_async(
            action,
            f"Think level for {endpoint_id or 'the active endpoint'} updated to {resolved}. "
            "This applies to new turns only.",
        )

    async def _handle_show_model_async(self, action: ControlAction) -> None:
        llm_runtime = self.context.require_port("llm:llm")
        self._refresh_llm_runtime_settings(llm_runtime)
        endpoints = self._llm_model_endpoints(llm_runtime)
        active_endpoint_id = self._effective_llm_endpoint_id(llm_runtime, endpoints)
        await self._deliver_control_delivery_async(
            control_interactions.model_panel_delivery(action.route, endpoints, active_endpoint_id)
        )

    async def _handle_set_model_async(self, action: ControlAction) -> None:
        requested = str(action.args.get("endpoint_id") or "").strip()
        if not requested:
            await self._complete_action_reply_async(action, "Use /model <endpoint_id>.")
            return
        llm_runtime = self.context.require_port("llm:llm")
        self._refresh_llm_runtime_settings(llm_runtime)
        endpoints = self._llm_model_endpoints(llm_runtime)
        endpoint = next(
            (item for item in endpoints if self._llm_endpoint_field(item, "endpoint_id") == requested),
            None,
        )
        if endpoint is None:
            known = ", ".join(self._llm_endpoint_field(item, "endpoint_id") for item in endpoints)
            message = f"Unknown enabled model endpoint: {requested}."
            if known:
                message = f"{message}\nAvailable endpoints: {known}"
            await self._complete_action_reply_async(action, message)
            return
        set_active_endpoint = getattr(llm_runtime, "set_active_endpoint", None)
        if callable(set_active_endpoint):
            set_active_endpoint(requested)
        else:
            settings_repository = getattr(llm_runtime, "settings_repository", None)
            set_active_setting = getattr(settings_repository, "set_active_llm_endpoint_id", None)
            if not callable(set_active_setting):
                await self._complete_action_reply_async(action, "Model switching is unavailable.")
                return
            set_active_setting(requested)
        self._refresh_llm_runtime_settings(llm_runtime)
        model_message = (
            f"Model updated to {self._llm_model_label(endpoint)}. "
            "This applies to new turns only."
        )
        status_builder = getattr(llm_runtime, "thinking_status", None)
        think_status = (
            status_builder(requested)
            if callable(status_builder)
            else {"available": False, "choices": []}
        )
        if control_interactions.is_interaction_action(action) and list(think_status.get("choices") or []):
            await self._deliver_control_delivery_async(
                control_interactions.think_panel_delivery(
                    action.route,
                    think_status,
                    banner=model_message,
                    back_to_models=True,
                )
            )
            return
        await self._complete_action_reply_async(
            action,
            model_message,
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
        dependent_refreshes: dict[str, dict[str, Any]] = {}
        dependent_refresh_errors: dict[str, str] = {}
        refreshed_ports: set[int] = {id(llm_runtime)}
        for port_name, port in self.context.port_registry.items():
            if id(port) in refreshed_ports:
                continue
            refresh_dependent = getattr(port, "refresh_llm_endpoints", None)
            if not callable(refresh_dependent):
                continue
            refreshed_ports.add(id(port))
            try:
                result = refresh_dependent()
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, dict):
                    dependent_refreshes[port_name] = dict(result)
            except Exception as exc:
                dependent_refresh_errors[port_name] = f"{exc.__class__.__name__}: {exc}"
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
        refreshed_dependents = [
            name
            for name, result in dependent_refreshes.items()
            if bool(result.get("refreshed"))
        ]
        cold_dependents = [
            name
            for name, result in dependent_refreshes.items()
            if not bool(result.get("refreshed"))
        ]
        if refreshed_dependents:
            lines.append(f"Dependent LLM runtimes refreshed: {', '.join(refreshed_dependents)}")
        if cold_dependents:
            lines.append(
                "Dependent LLM runtimes not loaded yet: "
                + ", ".join(cold_dependents)
            )
        for port_name, error in dependent_refresh_errors.items():
            lines.append(f"Dependent LLM runtime refresh failed ({port_name}): {error}")
        await self._complete_action_reply_async(action, "\n".join(lines))

    @staticmethod
    def _refresh_llm_runtime_settings(llm_runtime: Any) -> None:
        refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
        if callable(refresh):
            refresh()

    @staticmethod
    def _llm_model_endpoints(llm_runtime: Any) -> list[Any]:
        resolver = getattr(llm_runtime, "endpoint_resolver", None)
        enabled = getattr(resolver, "enabled", None)
        if callable(enabled):
            endpoints = list(enabled())
        else:
            endpoints = list(getattr(resolver, "endpoints", ()) or [])
        return [endpoint for endpoint in endpoints if PalCore._llm_endpoint_field(endpoint, "endpoint_id")]

    @staticmethod
    def _effective_llm_endpoint_id(llm_runtime: Any, endpoints: list[Any]) -> str:
        active = str(getattr(llm_runtime, "active_endpoint_id", "") or "").strip()
        if active:
            return active
        primary = getattr(getattr(llm_runtime, "endpoint_resolver", None), "primary", None)
        if callable(primary):
            endpoint = primary()
            endpoint_id = PalCore._llm_endpoint_field(endpoint, "endpoint_id")
            if endpoint_id:
                return endpoint_id
        if endpoints:
            return PalCore._llm_endpoint_field(endpoints[0], "endpoint_id")
        return ""

    @staticmethod
    def _llm_endpoint_field(endpoint: Any, field_name: str) -> str:
        if endpoint is None:
            return ""
        if isinstance(endpoint, dict):
            value = endpoint.get(field_name)
        else:
            value = getattr(endpoint, field_name, None)
        return str(value or "").strip()

    @staticmethod
    def _llm_model_label(endpoint: Any) -> str:
        endpoint_id = PalCore._llm_endpoint_field(endpoint, "endpoint_id")
        display_name = PalCore._llm_endpoint_field(endpoint, "display_name")
        model_id = PalCore._llm_endpoint_field(endpoint, "model_id")
        label = display_name or model_id or endpoint_id
        if endpoint_id and label != endpoint_id:
            return f"{label} ({endpoint_id})"
        return label or endpoint_id

    def artifact_scope_for_turn(self, turn_id: str | None) -> str | None:
        normalized = str(turn_id or "").strip()
        if not normalized:
            return None
        if normalized in self.state.active_turns or normalized in self.state.turn_scopes:
            return self.state.resident_execution_lifetime_id
        return None

    def capture_delivery_binding(self, turn_id: str | None) -> dict[str, Any]:
        normalized = str(turn_id or "").strip()
        continuation = self.state.active_turns.get(normalized)
        if not isinstance(continuation, TurnContinuation):
            return {}
        binding = continuation.delivery_binding
        if binding is None:
            return {}
        return {
            "channel_id": binding.endpoint.endpoint_id,
            "channel_kind": binding.endpoint.channel_kind,
            "reply_target": dict(binding.response_handle.reply_target),
            "control_scope_key": binding.control_scope_key,
        }

    async def _prepare_channel_artifacts_async(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> ChannelEnvelope:
        # Direct core callers (mostly tests and embedded hosts) share the same
        # idempotent boundary as ChannelRuntime.emit.
        from pal.channel.ingress import ChannelIngressCompiler

        return ChannelIngressCompiler(
            artifact_manager=self.context.port_registry.get("artifact:artifact"),
            scope_key=self.state.resident_execution_lifetime_id,
        ).compile(channel_envelope)

    async def _prepare_channel_turn_async(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> ChannelEnvelope:
        # Capture routing authority while the provider-normalized payload is
        # still intact. Typed ingress compilation is content-only and must not
        # be asked to reconstruct channel/control scope later.
        return await self._prepare_channel_artifacts_async(channel_envelope)

    async def _handle_show_log_async(self, action: ControlAction) -> None:
        await self._deliver_control_delivery_async(
            control_interactions.log_panel_delivery(action.route, bool(self.state.prompt_log_enabled))
        )

    async def _handle_set_log_async(self, action: ControlAction) -> None:
        enabled = bool(action.args.get("prompt_log_enabled"))
        self.state.prompt_log_enabled = enabled
        updated_ports: set[int] = set()
        for port_name, port in self.context.port_registry.items():
            if id(port) in updated_ports:
                continue
            setter = getattr(port, "set_prompt_log_enabled", None)
            if not callable(setter):
                continue
            updated_ports.add(id(port))
            try:
                result = setter(enabled)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self.state.diagnostics.append(
                    {
                        "kind": "runtime.prompt_log.dependent_update_failed",
                        "port": str(port_name),
                        "enabled": enabled,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
        message = (
            "Debug logging enabled for new turns and Bunshin role runs."
            if enabled
            else "Debug logging disabled for new turns and Bunshin role runs."
        )
        await self._complete_action_reply_async(action, message)

    async def _handle_interrupt_turn_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        interrupted = await self.turn_manager.interrupt_active_turn(reason="interrupted")
        message = "Interrupted the current turn." if interrupted else "No active turn to interrupt."
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
        reset_applied = await self._execute_soft_reset_async(request)
        scope_state.pending_requests.pop("reset_confirm", None)
        await self._complete_action_reply_async(
            action,
            (
                "Soft reset complete. L1/L2 and working memory projection were cleared."
                if reset_applied
                else "Soft reset was not applied: the active turn did not drain before the timeout."
            ),
        )

    async def _execute_soft_reset_async(self, request) -> bool:
        """Reset only after the active logical turn has actually exited.

        Interrupting a worker task is a request, not proof that its cleanup is
        complete.  If the resident does not drain within the bounded wait,
        leave its runtime state intact and let its own exit path resume the
        queued turn later.
        """
        async with self.state.channel_turn_transition_lock:
            if self.state.resident_quiescing:
                return False
            self.state.resident_quiescing = True
            self.state.resident_drained_event = asyncio.Event()
            current_turn_id = self.turn_manager.latest_active_turn_id()
            if current_turn_id is None:
                self.state.resident_drained_event.set()
        if current_turn_id is not None:
            await self.turn_manager.interrupt_active_turn(reason="reset")
        try:
            try:
                await asyncio.wait_for(
                    self.state.resident_drained_event.wait(),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                return False
            from pal.core.runtime_state import RuntimeSnapshotCoordinator

            coordinator = RuntimeSnapshotCoordinator(self.context.module_registry)
            if any(
                handle.runtime_state_port is not None
                for handle in self.context.module_registry.modules.values()
            ):
                await coordinator.reset("soft_reset")
            else:
                memory_service = self.context.require_port("memory:memory")
                soft_reset = getattr(memory_service, "asoft_reset", None)
                if callable(soft_reset):
                    await soft_reset()
                else:
                    sync_reset = getattr(memory_service, "soft_reset", None)
                    if callable(sync_reset):
                        await asyncio.to_thread(sync_reset)
            return True
        finally:
            async with self.state.channel_turn_transition_lock:
                self.state.resident_quiescing = False
                self.state.resident_drained_event.set()
            await self._start_next_queued_turn_async()

    async def _handle_compact_memory_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        self.cache_warm_deadline.cancel()
        if self.turn_manager.latest_active_turn_id() is not None:
            await self._complete_compact_reply_async(
                action,
                "Compaction is unavailable while a conversation turn is active. Try again after the current turn finishes.",
            )
            return
        memory_service = self.context.require_port("memory:memory")
        l1_items = list(
            getattr(getattr(memory_service, "l1_store", None), "items", ())
            or ()
        )
        if not l1_items:
            await self._complete_compact_reply_async(action, "Nothing to compact - memory is already minimal.")
            return
        run_result = await self.turn_executor.compact_memory_async(
            memory_service,
            target_input_budget=8192,
            reserved_output_tokens=4096,
        )
        if not run_result.success:
            await self._complete_compact_reply_async(
                action,
                "Compaction failed - memory state was left unchanged.",
            )
            return
        result = run_result.memory_result
        entry_count = getattr(result, "metadata", {}).get("projected_entry_count", 0) if result else 0
        summary_count = getattr(result, "metadata", {}).get("compact_summary_count", 0) if result else 0
        retired = getattr(result, "metadata", {}).get("retired_count", 0) if result else 0
        storage_text = "L1 compact summary updated." if summary_count else "No compact summary was stored."
        await self._complete_compact_reply_async(
            action,
            f"Context compacted. {storage_text} {entry_count} L2 entries projected, {retired} retired to L3.",
        )
        memory_candidates = memory_candidates_from_compact_result(result)
        if memory_candidates:
            source_ref = f"compact_{uuid4().hex[:12]}"
            delivery = memory_candidate_approval_delivery(
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

    async def handle_cache_warm_deadline_ignore(
        self,
        action: ControlAction,
    ) -> dict[str, Any]:
        epoch = str(action.args.get("cache_epoch") or "").strip()
        ignored = self.cache_warm_deadline.ignore(epoch)
        message = (
            "已忽略本轮热缓存 compact 提醒。"
            if ignored
            else "这条热缓存提醒已经失效。"
        )
        return {
            "delivery": control_interactions.terminal_delivery_for_action(
                action,
                message,
            )
        }

    async def handle_cache_warm_deadline_disable(
        self,
        action: ControlAction,
    ) -> dict[str, Any]:
        try:
            self.cache_warm_deadline.configure(enabled=False)
            message = "已关闭热缓存到期前的 compact 提醒。"
        except Exception as exc:
            message = f"关闭热缓存提醒失败：{exc}"
        return {
            "delivery": control_interactions.terminal_delivery_for_action(
                action,
                message,
            )
        }

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
        require_provider: bool = False,
    ) -> bool:
        route = delivery.route or fallback_route
        if delivery.delivery_kind == "reply":
            text = delivery.text or str(delivery.payload.get("text") or "")
            if text:
                if not require_provider:
                    return await self._reply_to_route_async(route, text)
                return await self._reply_to_route_async(
                    route, text, require_provider=require_provider
                )
            return True
        if delivery.delivery_kind == "endpoint_status":
            channel_runtime = self.context.port_registry.get("channel:channel")
            queue_endpoint_status = getattr(channel_runtime, "queue_endpoint_status", None)
            if not callable(queue_endpoint_status):
                return not require_provider
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
                if require_provider:
                    endpoint = channel_runtime.get_endpoint(endpoint_id)
                    if endpoint is None or not endpoint.attached or not endpoint.enabled:
                        return False
                result = queue_endpoint_status(endpoint_id, status_kind, payload=dict(status_payload))
                return result is not None or not require_provider
            return not require_provider
        if delivery.delivery_kind == "attachment":
            envelope = self._route_to_channel_envelope(
                route,
                require_provider=require_provider,
            )
            if envelope is None:
                return False
            path = Path(str(delivery.payload.get("path") or "")).expanduser()
            if not path.is_file():
                return False
            channel_runtime = self.context.port_registry.get("channel:channel")
            queue_attachment = getattr(channel_runtime, "queue_attachment", None)
            if not callable(queue_attachment):
                return not require_provider
            queue_attachment(
                self._delivery_binding_for_envelope(envelope),
                AttachmentSpec(
                    path=str(path.resolve()),
                    caption=str(delivery.payload.get("caption") or ""),
                    file_name=str(delivery.payload.get("file_name") or path.name),
                    mime_type=str(delivery.payload.get("mime_type") or ""),
                ),
            )
            return True
        if delivery.delivery_kind in control_interactions.INTERACTIVE_DELIVERY_KINDS:
            if delivery.interaction is None:
                if delivery.text:
                    if not require_provider:
                        return await self._reply_to_route_async(route, delivery.text)
                    return await self._reply_to_route_async(
                        route,
                        delivery.text,
                        require_provider=require_provider,
                    )
                return True
            payload = dict(delivery.payload)
            payload["spec"] = delivery.interaction
            if delivery.text:
                payload["text"] = delivery.text
            if not require_provider:
                return await self._status_to_route_async(
                    route,
                    delivery.delivery_kind,
                    payload,
                )
            return await self._status_to_route_async(
                route,
                delivery.delivery_kind,
                payload,
                require_provider=require_provider,
            )
        return True

    async def _reply_to_route_async(
        self,
        route: ControlRoute | None,
        text: str,
        *,
        require_provider: bool = False,
    ) -> bool:
        envelope = self._route_to_channel_envelope(
            route,
            require_provider=require_provider,
        )
        if envelope is None:
            return False
        channel_runtime = self.context.require_port("channel:channel")
        channel_runtime.queue_reply(
            self._delivery_binding_for_envelope(envelope),
            text,
        )
        return True

    async def _status_to_route_async(
        self,
        route: ControlRoute | None,
        kind: str,
        payload: dict[str, Any],
        *,
        require_provider: bool = False,
    ) -> bool:
        envelope = self._route_to_channel_envelope(
            route,
            require_provider=require_provider,
        )
        if envelope is None:
            return False
        channel_runtime = self.context.require_port("channel:channel")
        channel_runtime.queue_status(
            self._delivery_binding_for_envelope(envelope),
            kind,
            payload=payload,
        )
        return True

    def _route_from_channel_envelope(self, channel_envelope: ChannelEnvelope) -> ControlRoute:
        return route_from_channel_envelope(channel_envelope)

    def _route_to_channel_envelope(
        self,
        route: ControlRoute | None,
        *,
        require_provider: bool = False,
    ) -> ChannelEnvelope | None:
        if route is None:
            return None
        channel_runtime = self.context.port_registry.get("channel:channel")
        endpoint = channel_runtime.get_endpoint(route.endpoint_id) if channel_runtime is not None else None
        if require_provider:
            if endpoint is None or not endpoint.attached or not endpoint.enabled:
                return None
            inspect_health = getattr(endpoint, "inspect_health", None)
            health = inspect_health() if callable(inspect_health) else {}
            if isinstance(health, dict) and health.get("healthy") is False:
                return None
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
        opening_binding = TurnDeliveryBinding(
            endpoint=endpoint_config,
            response_handle=response_handle,
            control_scope_key=route.control_scope_key,
            correlation_id=route.correlation_id,
        )
        return ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.CONTROL_ACTION,
                source_kind=SourceKind.CONTROL,
                payload={},
                correlation_id=route.correlation_id,
            ),
            endpoint=endpoint_config,
            response_handle=response_handle,
            opening_delivery_binding=opening_binding,
        )

    async def _run_turn_continuation_async(self, continuation: TurnContinuation) -> TurnOutcome:
        current: EffectResult | None = None
        try:
            await self.turn_executor._ensure_l1_turn_async(
                continuation,
                PromptAssemblyContext(
                    event=continuation.opening_event,
                    core_mode=self.state.mode,
                ),
            )
            while True:
                if continuation.interrupted:
                    raise asyncio.CancelledError(continuation.interrupt_reason or "interrupted")
                yielded = self.turn_manager.resume(continuation, current)
                if isinstance(yielded, TurnOutcome):
                    outcome = yielded
                    await self._schedule_post_turn_commit_async(
                        outcome,
                        event=continuation.opening_event,
                        delivery_binding=continuation.delivery_binding,
                    )
                    await self._deliver_pending_compact_memory_candidates_async(continuation)
                    self.turn_executor.clear_execution_cursors(continuation)
                    return outcome
                current = await self._execute_turn_effect_async(continuation, yielded)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.turn_manager.commit_l1_exit_checkpoint_async(
                continuation,
                kind=L1MessageKind.TURN_ABORTED,
                status="aborted",
                reason=f"{exc.__class__.__name__}: {exc}",
            )
            raise

    async def _deliver_pending_compact_memory_candidates_async(self, continuation: TurnContinuation) -> None:
        batches = list(getattr(continuation, "pending_compact_memory_candidate_batches", []) or [])
        if not batches:
            return
        binding = continuation.delivery_binding
        if binding is None:
            self.state.diagnostics.append(
                {
                    "kind": "memory.compact_candidates.delivery_authority_missing",
                    "turn_id": continuation.turn_id,
                    "batch_count": len(batches),
                }
            )
            return
        continuation.pending_compact_memory_candidate_batches.clear()
        route = ControlRoute(
            endpoint_id=binding.endpoint.endpoint_id,
            channel_kind=binding.endpoint.channel_kind,
            reply_target=dict(binding.response_handle.reply_target),
            control_scope_key=binding.control_scope_key,
            correlation_id=binding.correlation_id,
        )
        for batch in batches:
            candidates = coerce_memory_candidate_list(batch.get("memory_candidates") if isinstance(batch, dict) else None)
            if not candidates:
                continue
            source_ref = f"compact_{uuid4().hex[:12]}"
            delivery = memory_candidate_approval_delivery(
                {
                    "source_kind": str(batch.get("source_kind") or "pal_compact"),
                    "source_ref": source_ref,
                    "source_label": str(batch.get("source_label") or "Pal compact"),
                    "candidate_batch_id": source_ref,
                    "memory_candidates": candidates,
                },
                route,
            )
            if delivery is None:
                self.state.diagnostics.append(
                    {
                        "kind": "memory.compact_candidates.delivery_missing",
                        "turn_id": continuation.turn_id,
                        "candidate_count": len(candidates),
                    }
                )
                continue
            await self._deliver_control_delivery_async(delivery, fallback_route=route)

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
        request: LLMRequestIR,
    ) -> LLMGenerationResult:
        return asyncio.run(self._stream_llm_request_async(continuation, llm_runtime, request))

    async def _stream_llm_request_async(
        self,
        continuation: TurnContinuation,
        llm_runtime,
        request: LLMRequestIR,
    ) -> LLMGenerationResult:
        return await self.turn_executor.stream_llm_request_async(continuation, llm_runtime, request)

    def _build_turn_prompt(
        self,
        continuation: TurnContinuation,
        assembly_context: PromptAssemblyContext,
        *,
        max_output_tokens: int,
    ) -> LLMRequestIR:
        return self.turn_executor.build_turn_prompt(
            continuation,
            assembly_context,
            max_output_tokens=max_output_tokens,
        )

    def _fallback_final_reply(self, continuation: TurnContinuation) -> str:
        return self.turn_executor.fallback_final_reply(continuation)

    def _infer_response_mode(self, outcome: LLMGenerationResult | None, *, used_tools: bool) -> str:
        return self.turn_executor.infer_response_mode(outcome, used_tools=used_tools)

    def _select_turn_temperature(self, response_mode: str) -> float:
        return self.turn_executor.select_turn_temperature(response_mode)

    def _debug_log_prompt(self, *args) -> None:
        if not self._prompt_log_enabled_from_args(*args):
            return
        request = _last_arg_of_type(args, LLMRequestIR)
        if request is None:
            return
        self._append_prompt_log(render_prompt_debug_log(request, context=self._prompt_debug_context(*args)))

    def _debug_log_outcome(self, *args) -> None:
        if not self._prompt_log_enabled_from_args(*args):
            return
        outcome = _last_arg_of_type(args, LLMGenerationResult)
        if outcome is None:
            return
        provider_payload = summarize_last_provider_payload(self.context.port_registry.get("llm:llm"))
        self._append_prompt_log(
            render_llm_outcome_debug_log(
                outcome,
                provider_payload=provider_payload,
                context=self._prompt_debug_context(*args),
            )
        )

    def _debug_log_reply(self, *args) -> None:
        if not self._prompt_log_enabled_from_args(*args):
            return
        text = str(args[-1] if args else "")
        self._append_prompt_log(render_reply_debug_log(text, context=self._prompt_debug_context(*args)))

    @staticmethod
    def _prompt_debug_context(*args) -> dict[str, Any]:
        for item in args:
            if isinstance(item, TurnContinuation):
                return {"turn_id": item.turn_id}
        return {}

    def _prompt_log_enabled_from_args(self, *args) -> bool:
        for item in args:
            if isinstance(item, TurnContinuation):
                return bool(item.turn_settings_snapshot.get("prompt_log_enabled"))
        request = _last_arg_of_type(args, LLMRequestIR)
        if request is not None and "prompt_log_enabled" in request.metadata:
            return bool(request.metadata.get("prompt_log_enabled"))
        return bool(self.state.prompt_log_enabled)

    def _append_prompt_log(self, text: str) -> None:
        root = getattr(self.config, "runtime_root", None)
        if root is None:
            print(text)
            return
        log_path = pal_log_path(Path(root))
        append_prompt_debug_log(log_path, text)

    def _schedule_post_turn_commit(
        self,
        outcome: TurnOutcome,
        *,
        event: EventEnvelope | None = None,
    ) -> None:
        asyncio.run(
            self._schedule_post_turn_commit_async(
                outcome,
                event=event,
            )
        )

    async def _schedule_post_turn_commit_async(
        self,
        outcome: TurnOutcome,
        *,
        event: EventEnvelope | None = None,
        delivery_binding: TurnDeliveryBinding | None = None,
    ) -> None:
        result = await self.turn_executor.schedule_post_turn_commit_async(outcome)
        if (
            self.context.port_registry.get("memory:memory") is not None
            and str(getattr(result, "state", "")) != "settled"
        ):
            raise RuntimeError(
                "Pal L1 working-set settlement failed; refusing to complete "
                "the logical turn"
            )
        if (
            str(getattr(result, "state", "")) == "settled"
            and event is not None
            and str(event.event_kind or "") == EventKind.USER_MESSAGE
            and str(event.source_kind or "") == SourceKind.CHANNEL
        ):
            self.state.compaction_user_turn_count += 1
            if delivery_binding is not None:
                try:
                    self.cache_warm_deadline.schedule_after_turn_commit(
                        route=ControlRoute(
                            endpoint_id=delivery_binding.endpoint.endpoint_id,
                            channel_kind=delivery_binding.endpoint.channel_kind,
                            reply_target=dict(
                                delivery_binding.response_handle.reply_target
                            ),
                            control_scope_key=delivery_binding.control_scope_key,
                            correlation_id=delivery_binding.correlation_id,
                        ),
                        turn_id=str(outcome.commit_payload.turn_id),
                    )
                except Exception as exc:
                    self.state.diagnostics.append(
                        {
                            "kind": "cache_warm_deadline.schedule_failed",
                            "turn_id": str(outcome.commit_payload.turn_id),
                            "error": f"{exc.__class__.__name__}: {exc}",
                        }
                    )

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
