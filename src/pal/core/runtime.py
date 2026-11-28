from __future__ import annotations

from pal.llm.contracts import LLM_RUNTIME
from pal.memory.contracts import MEMORY

import asyncio
import contextlib
import inspect
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pal.control import interactions as control_interactions
from pal.core import control_presentation as core_interactions
from pal.control.contracts import ControlAction, ControlDelivery, ControlRoute
from pal.core.memory_maintenance import MemoryMaintenanceMixin, SLEEP_REPLY
from pal.core.control_runtime import CoreControlRuntime
from pal.core.compaction_coordinator import (
    CompactionGate,
    CompactionPhase,
    CompactionTrigger,
    CompactionTicket,
    compaction_gate_active,
)
from pal.llm.compaction_engine import CompactionRunResult
from pal.memory.persistence.contracts import RESIDENT_HISTORY
from pal.memory.contracts import MemoryCompactResult

RESIDENT_COMPACTION_SCOPE = "pal:resident"
from pal.control.routing import derive_control_scope_key, route_from_channel_envelope
from pal.core.agent_turn_runtime import AgentTurnRuntime
from pal.core.contracts import CoreRuntimeState
from pal.core.scheduling import (
    CoreTurnPhase, ResidentTurnKind, SchedulingEvent, TurnLease, admission_reason,
)
from pal.core.runtime_config import RuntimeConfig
from pal.core.dispatcher import EventDispatcher
from pal.failure.contracts import FAILURE_RECOVERY, FailureHandlingResult, FailureRecoveryPort
from pal.failure.presentation import render_failure_feedback_text
from pal.shared.ports import ServiceRef
from pal.core.main_context import MainContext
from pal.core.timers import TIMER_EVENT, TimerScheduler
from pal.core.module_lifecycle import ModuleLifecycle
from pal.core.prompt_debug_log import (
    append_prompt_debug_log,
    render_llm_outcome_debug_log,
    render_prompt_debug_log,
    render_reply_debug_log,
    summarize_last_provider_payload,
)
from pal.core.prompt_compiler import PromptCompiler
from pal.llm.resident_compaction import PalCompactionPolicy
from pal.core.tool_stagnation import ToolStagnationGuardProcess
from pal.core.tool_surface import ToolSurface
from pal.core.turn_executor import TurnExecutor
from pal.core.turn_events import TURN_END, TURN_START
from pal.core.turns import EffectRequest, EffectResult, TurnContinuation, TurnOutcome, L1CommitPayload, channel_turn_program
from pal.core.module_registry import ModuleHandle
from pal.execution import CapabilityCall
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.foundation import AttachmentSpec, EventEnvelope, utc_now
from pal.foundation.log_paths import pal_log_path
from pal.foundation.diagnostics import exception_report, exception_reason
from pal.failure import FailureSignal, FailureUserFeedback
from pal.llm.contracts import LLMGenerationResult
from pal.llm.ir import LLMRequestIR, LLMMessageIR
from pal.memory import L1MessageKind, L1TranscriptMessage, MemoryCommitRequest
from pal.memory.compact import coerce_memory_candidate_list, memory_candidates_from_compact_result, compact_normalization_diagnostics
from pal.memory.mutations import content_hash
from pal.memory.tool_protocol import l1_tool_protocol_transcript
from pal.shared import ChannelEnvelope, EventKind, SourceKind, TurnDeliveryBinding
from pal.shared import IntrospectionPort, PromptAssemblyContext, PromptFragment, RuntimeStatus, ToolExecutionResult
from pal.shared.payloads import extract_text_from_payload


@dataclass
class PendingTurnClose:
    continuation: TurnContinuation
    state: str
    reason: str
    context_messages: tuple[LLMMessageIR, ...] = ()
    last_error: str = ""


@dataclass
class TurnManager:
    context: MainContext
    state: CoreRuntimeState
    guard: ToolStagnationGuardProcess = field(default_factory=ToolStagnationGuardProcess)
    config: RuntimeConfig = field(default_factory=RuntimeConfig.defaults)
    pending_closures: dict[str, PendingTurnClose] = field(default_factory=dict)

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
        llm_runtime = self.context.port_registry.optional(LLM_RUNTIME)
        if llm_runtime is None:
            return self.config.fallback_max_output_tokens
        return llm_runtime.resolve_output_budget(preferred_endpoint_id=preferred_endpoint_id)

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
            lease = self.state.scheduler.state.owner
            if lease is not None and lease.turn_id == continuation.turn_id:
                self.state.scheduler.transition(SchedulingEvent.PROGRAM_FINISHED, lease=lease)
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
            if (reason == "interrupted" and isinstance(continuation, TurnContinuation)
                    and (continuation.accepted_tool_batch_open or continuation.stop_after_tool_batch)):
                continuation.stop_after_tool_batch = True
                recovery = continuation.recovery_generation_task
                if recovery is not None and not recovery.done():
                    recovery.cancel()
                return True
            lease = self.state.scheduler.state.owner
            if lease is not None and lease.turn_id == turn_id:
                self.state.scheduler.transition(SchedulingEvent.CANCEL, lease=lease)
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
                try:
                    abort_result = output_port.abort_stream(continuation.delivery_binding.response_handle, reason=reason)
                    if inspect.isawaitable(abort_result):
                        await abort_result
                except Exception as exc:
                    # Presentation failure must not veto execution cancellation.
                    # Do not catch BaseException/CancelledError here.
                    self.state.diagnostics.append({
                        "kind": "channel.abort_stream_failed",
                        "turn_id": turn_id,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    })
            try:
                # Ordinary interrupts revoke model continuation only. Even a
                # detached tool/session handle is left alone; reset/shutdown
                # retain the explicit execution-cancellation path.
                if reason != "interrupted":
                    await self.context.execution_runtime.interrupt_turn(turn_id)
            finally:
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

    def cleanup_interrupted(self, turn_id: str, *, reason: str = "interrupted") -> bool:
        snapshots = self.context.execution_runtime.result_snapshots
        if snapshots is not None:
            snapshots.finish_turn(turn_id)
        self.context.execution_runtime.execution_sessions.clear_visible_results(turn_id)
        self.guard.clear(turn_id)
        continuation = self.state.active_turns.get(turn_id)
        if isinstance(continuation, TurnContinuation):
            continuation.interrupted = True
            continuation.interrupt_reason = reason
            request = self.pending_closures.get(turn_id) or PendingTurnClose(continuation, "interrupted", reason)
            if not self._close_memory_turn(request):
                return False
        self.state.active_turns.pop(turn_id, None)
        self.pending_closures.pop(turn_id, None)
        self.guard.clear(turn_id)
        self._mark_turn_exited(turn_id)
        return True

    def _close_memory_turn(self, request: PendingTurnClose) -> bool:
        turn_id = request.continuation.turn_id
        try:
            memory = self.context.port_registry.optional(MEMORY)
            if memory is not None and memory.active_l1_turn(turn_id) is not None:
                if request.state == "aborted":
                    memory.abort_l1_turn(turn_id, reason=request.reason, context_messages=request.context_messages)
                else:
                    memory.interrupt_l1_turn(turn_id, reason=request.reason, context_messages=request.context_messages)
        except Exception as exc:
            error = exception_report(exc)
            if request.last_error != error:
                self.state.diagnostics.append({"kind": f"memory.turn.{request.state}_failed",
                    "turn_id": turn_id, "error": error, "cleanup_pending": True})
                request.last_error = error
            self.pending_closures[turn_id] = request
            self.state.active_turns.setdefault(turn_id, request.continuation)
            return False
        self.pending_closures.pop(turn_id, None)
        return True

    def retry_pending_closures(self) -> None:
        for turn_id, request in tuple(self.pending_closures.items()):
            if self._close_memory_turn(request):
                self.state.active_turns.pop(turn_id, None)
                self.guard.clear(turn_id)
                self._mark_turn_exited(turn_id)

    def discard_pending_closures_after_reset(self) -> None:
        for turn_id in tuple(self.pending_closures):
            self.state.active_turns.pop(turn_id, None)
            self.guard.clear(turn_id)
            self._mark_turn_exited(turn_id)
        self.pending_closures.clear()

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
        llm = self.context.port_registry.optional(LLM_RUNTIME)
        if llm is not None:
            try:
                llm.end_prompt_cache_turn(continuation.turn_id)
            except Exception as exc:
                self.state.diagnostics.append({"kind": "llm.turn.close_failed",
                    "turn_id": continuation.turn_id, "error": exception_report(exc)})
        memory = self.context.port_registry.optional(MEMORY)
        history = self.context.port_registry.get(RESIDENT_HISTORY.name)
        request = PendingTurnClose(continuation, state, reason)
        try:
            if history is not None:
                history.finish_partial_stream(continuation.turn_id)
            from pal.memory.prompt_context import completed_tool_contexts
            active = memory.active_l1_turn(continuation.turn_id) if memory is not None else None
            request.context_messages = completed_tool_contexts(continuation, active) if active is not None else ()
        except Exception as exc:
            self.state.diagnostics.append({"kind": f"memory.turn.{state}_context_failed",
                "turn_id": continuation.turn_id, "error": exception_report(exc)})
        if not self._close_memory_turn(request):
            return
        if history is not None:
            try:
                if not await history.checkpoint_async(state):
                    raise RuntimeError("Interrupted history checkpoint was not saved")
            except Exception as exc:
                self.state.diagnostics.append({"kind": "resident.history.close_failed",
                    "turn_id": continuation.turn_id, "error": exception_report(exc)})

    def _build_turn_settings_snapshot(self) -> dict[str, Any]:
        llm = self.context.port_registry.optional(LLM_RUNTIME)
        settings = llm.snapshot_turn_settings().to_payload() if llm is not None else {}
        return {**settings, "prompt_log_enabled": self.state.prompt_log_enabled}

    def latest_active_turn_id(self) -> str | None:
        lease = self.state.scheduler.state.owner
        if lease is not None:
            self.state.resident_drained_event.clear()
            return lease.turn_id
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
        self.context.execution_runtime.execution_sessions.clear_visible_results(turn_id)
        lease = self.state.scheduler.state.owner
        if lease is not None and lease.turn_id == turn_id:
            self.state.scheduler.transition(SchedulingEvent.PROGRAM_FINISHED, lease=lease)
        elif self.state.active_turn_id == turn_id:
            self.state.active_turn_id = None
            self.state.resident_drained_event.set()
        self.state.turn_scopes.pop(turn_id, None)


@dataclass
class MainLoop:
    queue: deque[EventEnvelope] = field(default_factory=deque)
    dispatcher: EventDispatcher = field(default_factory=EventDispatcher)
    schedule_pending: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    timers: TimerScheduler = field(init=False, repr=False)
    _wakeup_event: asyncio.Event | None = field(default=None, init=False, repr=False)
    _wakeup_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.timers = TimerScheduler(self.notify_ready)

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
        timer_delay = self.timers.next_delay()
        if timer_delay is not None:
            timeout = timer_delay if timeout is None else min(timeout, timer_delay)
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
        for event in self.timers.drain_due():
            self.enqueue(event)
        self.drain_ready_sources(context)
        # Completion callbacks only release ownership and wake the pump. Queue
        # admission is a core action, never an asynchronous task's finally.
        if self.schedule_pending is not None:
            await self.schedule_pending()
        envelope = self.pop()
        if envelope is None:
            return None
        if envelope.event_kind == TIMER_EVENT:
            try:
                await envelope.payload.fire()
            except Exception as exc:
                state.diagnostics.append({
                    "kind": "runtime.timer.failed", "event_id": envelope.event_id,
                    "error": exception_report(exc),
                })
        else:
            for derived in await self.dispatcher.dispatch_async(envelope, context):
                self.enqueue(derived)
        if core_port is not None:
            core_port.state.memory_maintenance_changed.set()
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

    def llm_capabilities_for_turn(self, turn_id: str | None) -> dict[str, Any]:
        """Expose the same endpoint facts core uses for this turn's prompt projection."""
        continuation = self.core.state.active_turns.get(str(turn_id or ""))
        if continuation is None:
            return {}
        return self.core.turn_executor._resolve_llm_capabilities(continuation)

    def capture_delivery_binding(self, turn_id: str | None) -> dict[str, Any]:
        return self.core.capture_delivery_binding(turn_id)


@dataclass
class PalCore(MemoryMaintenanceMixin):
    context: MainContext
    state: CoreRuntimeState = field(default_factory=CoreRuntimeState)
    config: RuntimeConfig = field(default_factory=RuntimeConfig.defaults)
    main_loop: MainLoop = field(default_factory=MainLoop)
    turn_manager: TurnManager = field(init=False)
    prompt_compiler: PromptCompiler = field(init=False)
    tool_surface: ToolSurface = field(init=False)
    failure_recovery: ServiceRef[FailureRecoveryPort] = field(init=False)
    agent_turn_runtime: AgentTurnRuntime = field(init=False)
    turn_executor: TurnExecutor = field(init=False)
    module_lifecycle: ModuleLifecycle = field(init=False)
    control_runtime: CoreControlRuntime = field(init=False)
    _btw_turn: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.control_runtime = CoreControlRuntime(self)
        self.main_loop.schedule_pending = self._start_next_queued_turn_async
        self.turn_manager = TurnManager(
            context=self.context,
            state=self.state,
            guard=ToolStagnationGuardProcess.from_config(self.config),
            config=self.config,
        )
        self.tool_surface = ToolSurface(self.context)
        self.module_lifecycle = ModuleLifecycle(self.context, self.state)
        self.context.execution_runtime.lifecycle_controller = self
        self.failure_recovery = self.context.port_registry.observe(FAILURE_RECOVERY)
        self.agent_turn_runtime = AgentTurnRuntime.build(
            context=self.context,
            config=self.config,
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
            inject_pending=self._inject_pending_for_executor_async,
            after_compaction=self._after_compaction_async,
        )
        self.prompt_compiler = self.agent_turn_runtime.prompt_compiler
        self.turn_executor = self.agent_turn_runtime.executor
        self.context.execution_runtime.register_provider_ref("core:turn_io", CoreTurnIOPort(core=self), owner_id="core")

    def close(self) -> None:
        self.control_runtime.requests.close()
        self.main_loop.timers.close()
        btw = self._btw_turn
        if btw is not None and btw.task is not None:
            btw.task.cancel()

    async def _after_tool_batch_async(self, continuation: TurnContinuation) -> None:
        # Ordering (P1): batch results are already committed to L1 when this
        # hook runs. Queued interjections are no longer admitted here; the
        # preflight effect admits them only when the next ordinary request
        # is real and no compaction ticket is held, so fresh user input is
        # never fed into an imminent compaction source (PLAN section 4).
        return

    def _compaction_gate(self) -> CompactionGate:
        return CompactionGate(
            self.state,
            transition_lock=self.state.channel_turn_transition_lock,
            admission_barrier=self.state.scheduler.set_barrier,
        )

    def _compaction_gate_active(self) -> bool:
        return compaction_gate_active(self.state)

    async def _inject_pending_for_executor_async(self, continuation) -> bool:
        from pal.core.interjection import inject_pending_interjection_async

        owner = self.state.scheduler.state.owner
        if owner is not None and owner.kind != ResidentTurnKind.CHAT:
            return False
        before = len(self.state.pending_channel_turns)
        await inject_pending_interjection_async(
            context=self.context,
            state=self.state,
            continuation=continuation,
        )
        return len(self.state.pending_channel_turns) < before

    async def _after_compaction_async(self, continuation) -> None:
        # Drain queued input into the fresh context before the next
        # ordinary LLM request (PLAN section 4 default order).
        await self._inject_pending_for_executor_async(continuation)

    def event_loop(self) -> MainLoop:
        return self.main_loop

    def receive_event(self, envelope: EventEnvelope) -> None:
        self.main_loop.enqueue(envelope)

    def notify_ready(self) -> None:
        self.state.memory_maintenance_changed.set()
        self.main_loop.notify_ready()

    def bind_async_wakeup_sources(self) -> None:
        control_plane = self.context.port_registry.get("control:control")
        if control_plane is not None:
            control_plane.on_ready = self.notify_ready
        self.main_loop.bind_async_loop()
        channel_runtime = self.context.port_registry.get("channel:channel")
        if channel_runtime is not None and hasattr(channel_runtime, "on_ready"):
            channel_runtime.on_ready = self.notify_ready
    async def wait_for_ready_async(self, *, timeout: float | None = None) -> None:
        await self.main_loop.wait_for_ready_async(timeout=timeout)

    def next_wakeup_timeout_seconds(self) -> float | None:
        return self.main_loop.timers.next_delay()

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
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="turn_id is required",
                llm_text="Could not send attachment: turn_id is required.",
                structured={"reason": "turn_id_required", "error_code": "turn_id_required", "kind": "rejected", "retry": "correct_input"},
            )
        continuation = self.state.active_turns.get(normalized_turn_id)
        if not isinstance(continuation, TurnContinuation):
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="active turn not found",
                llm_text="Could not send attachment: active turn not found.",
                structured={"reason": "turn_not_active", "error_code": "turn_not_active", "kind": "rejected", "retry": "correct_input", "turn_id": normalized_turn_id},
            )
        if continuation.delivery_binding is None:
            return CapabilityResult(
                status=RuntimeStatus.UNSUPPORTED,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="active turn has no delivery authority",
                llm_text="Could not send attachment: active turn has no delivery authority.",
                structured={
                    "reason": "delivery_authority_missing", "error_code": "delivery_authority_missing", "kind": "rejected", "retry": "correct_input",
                    "turn_id": normalized_turn_id,
                },
            )
        path = Path(attachment.path).expanduser()
        if not path.is_file():
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text=f"attachment file not found: {path}",
                llm_text=f"Could not send attachment: file not found at {path}.",
                structured={"reason": "file_not_found", "error_code": "file_not_found", "kind": "rejected", "retry": "correct_input", "path": str(path)},
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
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                text="channel runtime does not support attachments",
                llm_text="Could not send attachment: channel runtime does not support attachments.",
                structured={"reason": "attachment_not_supported", "error_code": "attachment_not_supported", "kind": "rejected", "retry": "correct_input"},
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

    async def _deliver_busy_retry_notice_async(
        self, channel_envelope: ChannelEnvelope,
    ) -> None:
        """Q04: explicit BUSY_RETRY receipt for input queued behind a live
        compaction ticket.

        The queued envelope is already durable in the in-process queue; a
        failed receipt must never dequeue or duplicate it, so every error
        here is swallowed (the queue semantics in
        ``_schedule_admitted_channel_turn_async`` remain authoritative).
        """

        channel_runtime = self.context.port_registry.get("channel:channel")
        queue_reply = getattr(channel_runtime, "queue_reply", None)
        if not callable(queue_reply):
            return
        try:
            result = queue_reply(
                self._delivery_binding_for_envelope(channel_envelope),
                "A context compaction is in progress; your message is "
                "queued and has NOT entered the conversation yet. It will "
                "be handled as soon as the compaction finishes — no need "
                "to resend.",
            )
            if inspect.isawaitable(result):
                await result
        except Exception:
            diagnostics = getattr(self.state, "diagnostics", None)
            if diagnostics is not None:
                diagnostics.append({
                    "kind": "busy_retry_notice_failed",
                    "event_id": str(
                        getattr(channel_envelope.event, "event_id", "")
                    ),
                })

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

    def _resident_other_work(self) -> bool:
        self.turn_manager.retry_pending_closures()
        # /btw reads a captured L in its own branch; it is not a resident
        # writer. Legacy/stub hosts with live records still fail admission.
        return bool(
            any(key != self.state.btw_turn_id for key in self.state.active_turns)
            or any(key != self.state.btw_turn_id and not task.done()
                   for key, task in self.state.turn_tasks.items())
        )

    def _pending_user_work(self) -> bool:
        # The pump drains ALL ready sources before invoking any handler.
        # Source preparation is advisory; actual admission sees the normalized
        # user events already in the pump, irrespective of source ordering.
        return bool(
            self.state.pending_channel_turns or self.state.memory_ingress_reservations
            or any(event.event_kind == EventKind.USER_MESSAGE for event in self.main_loop.queue)
        )

    def resident_admission_reason(self, kind: ResidentTurnKind = ResidentTurnKind.CHAT) -> str:
        if self._compaction_gate_active():
            return "compaction"
        return admission_reason(
            self.state.scheduler.state, kind,
            pending_users=self._pending_user_work() if kind != ResidentTurnKind.CHAT else False,
            other_work=self._resident_other_work(),
        )

    def _admit_resident_turn(self, turn_id: str, kind: ResidentTurnKind) -> TurnLease | None:
        if self._compaction_gate_active():
            return None
        decision = self.state.scheduler.transition(
            SchedulingEvent.ADMIT, turn_id=turn_id, kind=kind,
            pending_users=self._pending_user_work() if kind != ResidentTurnKind.CHAT else False,
            other_work=self._resident_other_work(),
        )
        if not decision.accepted:
            return None
        self.turn_manager._remember_active_turn(turn_id)
        return decision.state.owner

    def _finish_resident_turn(self, lease: TurnLease) -> None:
        scheduler = self.state.scheduler
        scheduler.transition(SchedulingEvent.PROGRAM_FINISHED, lease=lease)
        if scheduler.transition(SchedulingEvent.TASK_FINISHED, lease=lease).accepted:
            if self.state.active_turn_id == lease.turn_id:
                self.state.active_turn_id = None
            self.state.resident_drained_event.set()
            self.notify_ready()

    def start_background_turn(
        self, continuation: TurnContinuation, run: Callable, *,
        owner: ModuleHandle | None,
        kind: ResidentTurnKind = ResidentTurnKind.BACKGROUND,
    ) -> asyncio.Task | None:
        """Reserve before starting any asynchronous producer or opening L1."""
        ownership = None
        if owner is not None:
            if not self.context.module_registry.is_current(owner):
                return None
            ownership = owner.background_work.acquire()
            if ownership is None:
                return None
        try:
            lease = self._admit_resident_turn(continuation.turn_id, kind)
        except BaseException:
            if ownership is not None:
                ownership.close()
            raise
        if lease is None:
            if ownership is not None:
                ownership.close()
            return None
        coroutine = None
        task = None
        try:
            coroutine = run()
            task = asyncio.create_task(coroutine)
            if ownership is not None:
                task.add_done_callback(lambda _, retained=ownership: retained.close())
            self.track_turn_task(continuation, task, lease=lease)
            return task
        except BaseException:
            if task is not None:
                # A failed registration still owns a scheduled coroutine.
                # Cancel it and keep the slot until cancellation is observed.
                task.cancel()
                task.add_done_callback(lambda finished:
                                       self._on_turn_task_done(lease.turn_id, finished, lease=lease))
            elif coroutine is not None:
                coroutine.close()
            if task is None:
                self._finish_resident_turn(lease)
                if ownership is not None:
                    ownership.close()
            raise

    def _start_channel_turn_task_locked(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> asyncio.Task[Any] | None:
        delivery_binding = channel_envelope.opening_delivery_binding
        if delivery_binding is None:
            control_scope_key = self._derive_channel_control_scope_key(channel_envelope)
            delivery_binding = TurnDeliveryBinding.from_envelope(
                channel_envelope,
                control_scope_key=control_scope_key,
            )
        control_scope_key = delivery_binding.control_scope_key
        turn_id = channel_envelope.event.event_id
        lease = self._admit_resident_turn(turn_id, ResidentTurnKind.CHAT)
        if lease is None:
            return None
        self.state.turn_scopes[turn_id] = control_scope_key
        coroutine = self._background_channel_turn_runner_async(channel_envelope)
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            self.state.turn_scopes.pop(turn_id, None)
            self._finish_resident_turn(lease)
            raise
        self.state.turn_tasks[turn_id] = task
        task.add_done_callback(lambda finished, owner=lease:
                              self._on_channel_turn_task_done(channel_envelope, finished, lease=owner))
        self.context.turn_event_bus.emit(TURN_START, {
            "turn_id": turn_id,
            "scope_key": control_scope_key,
            "endpoint_id": channel_envelope.endpoint.endpoint_id,
            "channel_kind": channel_envelope.endpoint.channel_kind,
            "reply_target": dict(channel_envelope.response_handle.reply_target),
        })
        return task

    async def _start_next_queued_turn_async(self) -> None:
        async with self.state.channel_turn_transition_lock:
            if self.resident_admission_reason():
                return
            while self.state.pending_channel_turns:
                pending = self.state.pending_channel_turns[0]
                next_envelope = await self._prepare_channel_turn_async(pending)
                next_turn_id = next_envelope.event.event_id
                if self._turn_task_running(next_turn_id):
                    self.state.pending_channel_turns.popleft()
                    continue
                if self._start_channel_turn_task_locked(next_envelope) is not None:
                    self.state.pending_channel_turns.popleft()
                return

    async def schedule_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> None:
        async with self.state.channel_turn_transition_lock:
            sleeping = self.state.memory_maintenance
            if not sleeping:
                self.state.memory_ingress_reservations += 1
                self.remember_user_route(channel_envelope)
        if sleeping:
            await self.deliver_memory_notice_async(self._route_from_channel_envelope(channel_envelope), SLEEP_REPLY, require_provider=False)
            return
        try:
            await self._schedule_admitted_channel_turn_async(channel_envelope)
        finally:
            self.state.memory_ingress_reservations -= 1
            self.state.memory_maintenance_changed.set()

    async def _schedule_admitted_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> None:
        # Any new user activity makes the old idle-cache deadline irrelevant,
        # even if this turn must queue behind the current one.
        await self.control_runtime.user_activity()
        channel_envelope = await self._prepare_channel_turn_async(channel_envelope)
        turn_id = channel_envelope.event.event_id
        async with self.state.channel_turn_transition_lock:
            if self._turn_task_running(turn_id) or self._channel_turn_is_pending(turn_id):
                # Duplicate arrival for a turn that is already running or
                # already queued: nothing to do. The active turn's
                # working/typing status must keep running.
                return
            if self.resident_admission_reason() or self.state.pending_channel_turns:
                # Busy: queue the envelope.
                # The active turn keeps its typing status; working_stop is
                # only emitted when that turn actually ends (turn.end /
                # runner finally). An interjection may later be injected
                # from this queue without ever starting its own turn, so
                # stopping typing here would leave the chat silently idle
                # while the tool chain is still running.
                self.state.pending_channel_turns.append(channel_envelope)
                if self._compaction_gate_active():
                    # Q04 BUSY_RETRY: an explicit, user-visible receipt —
                    # the message is NOT in the conversation yet.  It stays
                    # queued (no L/R write, no turn, no durable staging) and
                    # is admitted right after the compaction releases.
                    await self._deliver_busy_retry_notice_async(
                        channel_envelope,
                    )
                return
            self._start_channel_turn_task_locked(channel_envelope)

    def process_channel_turn(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        return asyncio.run(self.process_channel_turn_async(channel_envelope))

    async def process_channel_turn_async(
        self,
        channel_envelope: ChannelEnvelope,
    ) -> TurnOutcome:
        turn_id = channel_envelope.event.event_id
        tracked = self.state.turn_tasks.get(turn_id) is asyncio.current_task()
        async with self.state.channel_turn_transition_lock:
            if (
                self.state.resident_quiescing
                or self.state.memory_maintenance
                or self._compaction_gate_active()
            ):
                raise RuntimeError(
                    "resident runtime is quiescing, dreaming, or compacting"
                )
            lease = self.state.scheduler.state.owner if tracked else self._admit_resident_turn(
                turn_id, ResidentTurnKind.CHAT,
            )
            if lease is None or lease.turn_id != turn_id:
                raise RuntimeError("resident turn admission is busy")
            self.state.memory_ingress_reservations += 1
        try:
            return await self._process_admitted_channel_turn_async(channel_envelope)
        except BaseException:
            if not tracked:
                self.turn_manager.cleanup_interrupted(turn_id, reason="failed")
            raise
        finally:
            self.state.memory_ingress_reservations -= 1
            self.state.memory_maintenance_changed.set()
            if not tracked:
                self._finish_resident_turn(lease)
                await self._start_next_queued_turn_async()

    async def _process_admitted_channel_turn_async(
        self, channel_envelope: ChannelEnvelope,
    ) -> TurnOutcome:
        await self.control_runtime.user_activity()
        channel_envelope = await self._prepare_channel_turn_async(channel_envelope)
        # The hot path is: start turn -> interpret yielded effects -> resume
        # until the generator returns a TurnOutcome.
        continuation = self.turn_manager.start(
            channel_envelope,
            delivery_binding=channel_envelope.opening_delivery_binding,
        )
        self._begin_tool_result_turn(continuation)
        background_task = self.state.turn_tasks.get(
            channel_envelope.event.event_id
        )
        return await self._run_turn_continuation_async(
            continuation,
            notify_settled=(
                background_task is None
                or background_task is not asyncio.current_task()
            ),
        )

    def turn_execution_options(self) -> dict[str, Any]:
        return {
            "core_mode": self.state.mode,
            "max_output_tokens": self.turn_manager._resolve_max_output_tokens(),
        }

    def track_turn_task(
        self, continuation: TurnContinuation, task: asyncio.Task[Any], *, lease: TurnLease,
    ) -> None:
        if lease != self.state.scheduler.state.owner or lease.turn_id != continuation.turn_id:
            raise RuntimeError("tracked resident task requires its admission lease")
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
                lease=lease,
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
        *, lease: TurnLease,
    ) -> None:
        if lease != self.state.scheduler.state.owner:
            self._on_turn_task_done(continuation.turn_id, task, lease=lease)
            return
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
        self._on_turn_task_done(continuation.turn_id, task, lease=lease)

    async def run_turn_continuation_async(self, continuation: TurnContinuation) -> TurnOutcome:
        # Tracked callers already own paired events through their task callback.
        owns_events = self.state.turn_tasks.get(continuation.turn_id) is not asyncio.current_task()
        lease = self.state.scheduler.state.owner
        if owns_events:
            lease = self._admit_resident_turn(continuation.turn_id, ResidentTurnKind.CHAT)
        if lease is None or lease.turn_id != continuation.turn_id:
            raise RuntimeError("resident turn admission is busy")
        payload = self._tracked_turn_event_payload(continuation)
        if owns_events:
            self.context.turn_event_bus.emit(TURN_START, payload)
        status = "failed"
        try:
            self.state.active_turns[continuation.turn_id] = continuation
            self._begin_tool_result_turn(continuation)
            outcome = await self._run_turn_continuation_async(continuation)
            status = "success"
            return outcome
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            if owns_events:
                if continuation.turn_id in self.state.active_turns:
                    self.turn_manager.cleanup_interrupted(continuation.turn_id, reason=status)
                self.context.turn_event_bus.emit(TURN_END, {**payload, "status": status})
                self._finish_resident_turn(lease)
                await self._start_next_queued_turn_async()

    def _begin_tool_result_turn(self, continuation: TurnContinuation) -> None:
        execution = self.context.execution_runtime
        execution.bind_result_history(self.context.require_port(MEMORY))
        execution.begin_tool_result_turn(
            turn_id=continuation.turn_id,
            scope_key=self.state.resident_execution_lifetime_id,
            input_id=continuation.turn_id,
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
        outcome: TurnOutcome | None = None
        try:
            outcome = await self.process_channel_turn_async(channel_envelope)
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
            # This producer completed its presentation teardown even if Memory
            # still owns a pending close. The done callback only covers tasks
            # cancelled before this coroutine gets its first step.
            self.state.turn_scopes.pop(turn_id, None)
            if (
                turn_status == "success"
                and outcome is not None
            ):
                await self._notify_turn_settled_async(
                    outcome,
                    event=channel_envelope.event,
                    delivery_binding=delivery_binding,
                    notify_observers=not self.state.pending_channel_turns,
                )
            self._queue_channel_status(channel_envelope, "working_stop", {"typing_owner": f"turn:{turn_id}"})
            # The done callback releases the lease AFTER this finally exits.
            self.notify_ready()

    def _on_channel_turn_task_done(
        self, envelope: ChannelEnvelope, task: asyncio.Task[Any], *, lease: TurnLease,
    ) -> None:
        turn_id = envelope.event.event_id
        # A task cancelled before its first coroutine step never enters the
        # runner's finally. Close its presentation and reservation here too.
        if lease == self.state.scheduler.state.owner and turn_id in self.state.turn_scopes:
            self.turn_manager.cleanup_interrupted(turn_id, reason="interrupted" if task.cancelled() else "failed")
            self.context.turn_event_bus.emit(TURN_END, {
                "turn_id": turn_id,
                "scope_key": self._delivery_binding_for_envelope(envelope).control_scope_key,
                "endpoint_id": envelope.endpoint.endpoint_id,
                "channel_kind": envelope.endpoint.channel_kind,
                "reply_target": dict(envelope.response_handle.reply_target),
                "status": "interrupted" if task.cancelled() else "failed",
            })
            self._queue_channel_status(envelope, "working_stop", {"typing_owner": f"turn:{turn_id}"})
        self._on_turn_task_done(turn_id, task, lease=lease)

    def _on_turn_task_done(
        self, turn_id: str, task: asyncio.Task[Any], *, lease: TurnLease | None = None,
    ) -> None:
        stored = self.state.turn_tasks.get(turn_id)
        if stored is task:
            self.state.turn_tasks.pop(turn_id, None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        if lease is not None:
            self._finish_resident_turn(lease)
        self.notify_ready()

    async def handle_control_action_async(self, action: ControlAction, *, require_provider: bool = False) -> bool | None:
        spec = self.context.control_action_registry.resolve(action.action_kind)
        if (self.state.memory_maintenance and action.delivery is None
                and (spec is None or not spec.allow_during_maintenance)):
            await self.deliver_memory_notice_async(action.route, SLEEP_REPLY, require_provider=False)
            return False
        if not self.state.memory_maintenance and action.delivery is None and action.route is not None:
            channel = self.context.port_registry.get("channel:channel")
            remember = getattr(channel, "remember_user_route", None)
            if callable(remember):
                remember(action.route)
        self.state.memory_control_reservations += 1
        try:
            return await self._handle_admitted_control_action_async(action, require_provider=require_provider)
        finally:
            self.state.memory_control_reservations -= 1
            self.state.memory_maintenance_changed.set()

    async def _handle_admitted_control_action_async(
        self, action: ControlAction, *, require_provider: bool = False,
    ) -> bool | None:
        status_route = action.route or (action.delivery.route if action.delivery is not None else None)
        typing_payload = {"typing_owner": f"control:{uuid4().hex}"}
        with contextlib.suppress(Exception):
            await self._status_to_route_async(status_route, "typing_start", typing_payload)
        try:
            if action.delivery is not None:
                return await self._deliver_control_delivery_async(
                    action.delivery, fallback_route=action.route, require_provider=require_provider)
            if not await self.context.control_action_registry.handle(action):
                await self.control_runtime.reply(action, f"Control action '{action.action_kind}' is not registered.")
        finally:
            with contextlib.suppress(Exception):
                await self._status_to_route_async(status_route, "working_stop", typing_payload)

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

    async def _handle_show_status_async(self, action: ControlAction) -> None:
        active_turns = len(self.state.active_turns)
        queued_messages = len(self.state.pending_channel_turns)
        if self.state.resident_quiescing:
            activity = "quiescing (preparing to stop)"
        elif self.state.memory_maintenance:
            activity = "memory_maintenance (sleeping)"
        elif self.state.scheduler.state.phase == CoreTurnPhase.FINALIZING:
            activity = "finalizing (finishing a turn)"
        elif self.state.scheduler.state.owner is not None:
            activity = "in_turn (processing a turn)"
        elif active_turns:
            activity = "in_turn (processing a turn)"
        elif queued_messages:
            activity = "queued (waiting to start a turn)"
        else:
            activity = "idle"
        lines = [
            "🤖 Pal status",
            f"State: {activity}",
            f"Active turns: {active_turns}",
            f"Queued messages: {queued_messages}",
        ]
        for owner, status in tuple(self.context.control_action_registry.status_providers.items()):
            try:
                text = status().strip()
            except Exception as exc:
                text = f"{owner} status unavailable: {exception_reason(exc)}"
            if text:
                lines.extend(("", text))
        await self._deliver_control_delivery_async(
            control_interactions.terminal_delivery_for_action(action, "\n".join(lines))
        )


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
            core_interactions.log_panel_delivery(action.route, bool(self.state.prompt_log_enabled))
        )

    async def _handle_set_log_async(self, action: ControlAction) -> None:
        enabled = bool(action.args.get("prompt_log_enabled"))
        self.state.prompt_log_enabled = enabled
        errors = await self.control_runtime.notify("core.prompt_log.changed", {"enabled": enabled})
        message = ("Debug logging enabled for new turns and Bunshin role runs." if enabled
                   else "Debug logging disabled for new turns and Bunshin role runs.")
        if errors:
            message = (
                f"Core debug logging {'enabled' if enabled else 'disabled'} for new turns. "
                "Some dependent runtimes did not confirm the update; their state may differ.\n"
                + "\n".join(f"{owner}: {error}" for owner, error in errors.items())
            )
        await self.control_runtime.reply(action, message)

    async def _handle_interrupt_turn_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        active_turn_id = self.turn_manager.latest_active_turn_id()
        continuation = self.state.active_turns.get(active_turn_id)
        interrupted = await self.turn_manager.interrupt_active_turn(reason="interrupted")
        message = "Interrupted the current turn." if interrupted else "No active turn to interrupt."
        if interrupted and continuation is not None and continuation.stop_after_tool_batch and continuation.accepted_tool_batch_open:
            message = "Stop requested; finishing the accepted tool batch before ending this turn."
        if self.turn_manager.pending_closures:
            message = ("The turn stopped, but its conversation state could not be finalized. "
                       "New turns will wait for memory recovery; see diagnostics for the cause.")
        # Two-segment admission: /interrupt arbitrates against the compact
        # run on the history OWNER.  An idle manual run has no parent turn
        # and is never fake-cancelled (X03); a run whose parent turn was
        # interrupted loses commit eligibility before its summary returns
        # (X01); an install that already won stays (X02).
        memory_service = self.context.require_port(MEMORY)
        verdict = memory_service.interrupt_compaction_for_turn(active_turn_id)
        if verdict == "cancelled" and not interrupted:
            message = "Cancelled the running context compaction."
        await self._complete_action_reply_async(action, message)

    async def _handle_open_reset_confirm_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        from pal.core.contracts import PendingControlRequest

        requests = self.control_runtime.requests
        existing = requests.get(route.control_scope_key, "reset_confirm")
        if existing is not None:
            existing.expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
            requests.put(existing)
            await self._render_reset_prompt_async(existing)
            return
        request = PendingControlRequest(
            request_id=f"reset_{uuid4().hex[:12]}",
            request_kind="reset_confirm",
            control_scope_key=route.control_scope_key,
            route=route,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            payload={},
        )
        requests.put(request)
        await self._render_reset_prompt_async(request)

    async def _handle_cancel_reset_confirm_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        request_id = str(action.args.get("request_id") or action.args.get("interaction_id") or "").strip()
        requests = self.control_runtime.requests
        request = requests.get(route.control_scope_key, "reset_confirm")
        if request is not None and request.request_id == request_id:
            requests.remove(route.control_scope_key, "reset_confirm")
        await self._complete_action_reply_async(action, "Reset cancelled.")

    async def _handle_reset_memory_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        request_id = str(action.args.get("request_id") or "").strip()
        requests = self.control_runtime.requests
        request = requests.get(route.control_scope_key, "reset_confirm")
        if request is None or request.request_id != request_id:
            await self._complete_action_reply_async(action, "Reset request is missing, expired, or already consumed.")
            return
        if request.expired:
            await requests.expire(request)
            return
        reset_applied = await self._execute_soft_reset_async(request)
        requests.remove(route.control_scope_key, "reset_confirm")
        await self._complete_action_reply_async(
            action,
            (
                "Soft reset complete. L1/L2 and working memory projection were cleared."
                if reset_applied
                else "Soft reset was not applied. Inspect core diagnostics for a drain timeout or recovery storage failure."
            ),
        )

    async def _execute_soft_reset_async(self, request) -> bool:
        """Reset only after the active logical turn has actually exited.

        Interrupting a worker task is a request, not proof that its cleanup is
        complete.  If the resident does not drain within the bounded wait,
        leave its runtime state intact and let its own exit path resume the
        queued turn later.
        """
        reset_barrier = "quiescing:reset:" + uuid4().hex
        claimed = False
        try:
            async with self.state.channel_turn_transition_lock:
                if self.state.resident_quiescing:
                    return False
                self.state.scheduler.set_barrier(reset_barrier, True)
                claimed = True
                self.control_runtime.requests.invalidate_context()
                # Two-segment admission: a confirmed reset seizes the history
                # root's live run first (X04/X05); the owner cancels it before
                # any history is cleared, so a late summary cannot publish into
                # the new session.
                self.context.require_port(
                    "memory:memory"
                ).cancel_active_compaction(reason="reset")
                self.state.resident_drained_event = asyncio.Event()
                current_turn_id = self.turn_manager.latest_active_turn_id()
                if current_turn_id is None:
                    self.state.resident_drained_event.set()
            await self.control_runtime.notify("core.context_change.started", {"trigger": "reset"})
            from pal.core.btw import cancel_btw

            if current_turn_id is not None:
                await self.turn_manager.interrupt_active_turn(reason="reset")
            await cancel_btw(self)
            try:
                await asyncio.wait_for(
                    self.state.resident_drained_event.wait(),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                return False
            from pal.core.runtime_state import RuntimeSnapshotCoordinator

            coordinator = RuntimeSnapshotCoordinator(self.context.module_registry)
            history = self.context.port_registry.get(RESIDENT_HISTORY.name)
            if history is not None:
                try:
                    await history.reset_async()
                except Exception:
                    self.state.diagnostics.append({"kind": "resident.history.reset_failed"})
                    return False
            else:
                await coordinator.reset("soft_reset")
            self.turn_manager.discard_pending_closures_after_reset()
            # F6 (review af51d74): the LLM runtime's hosted projection
            # lineages must not outlive the history authority they were
            # frozen from — retire them with the rolled incarnation.
            llm_runtime = self.context.require_port(LLM_RUNTIME)
            projection = llm_runtime.projection_port
            if projection is not None:
                projection.retire_projection_sessions()
            return True
        finally:
            if claimed:
                async def finish_reset() -> None:
                    async with self.state.channel_turn_transition_lock:
                        self.state.scheduler.set_barrier(reset_barrier, False)
                        self.state.resident_drained_event.set()
                    await self._start_next_queued_turn_async()

                cleanup = asyncio.create_task(finish_reset())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated cancellation must not strand the admission
                    # barrier while its cleanup waits for the transition lock.
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
                    raise

    async def _handle_compact_memory_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        gate = self._compaction_gate()
        busy_reply: str | None = None
        ticket = None
        # Admission is one critical section: no active turn, no unfinished
        # turn teardown, no earlier pending input, no other quiesce owner,
        # all precede the ticket claim. The
        # manual intent is never scheduled for later (A06/A08/A10).
        async with self.state.channel_turn_transition_lock:
            if self.turn_manager.latest_active_turn_id() is not None:
                busy_reply = (
                    "Compaction is unavailable while a conversation turn is active. "
                    "Try again after the current turn finishes."
                )
            elif any(not task.done() for task in self.state.turn_tasks.values()):
                busy_reply = (
                    "Compaction is unavailable while a previous turn is still "
                    "finishing. Try again in a moment."
                )
            elif self.state.pending_channel_turns:
                busy_reply = (
                    "Compaction is postponed: earlier incoming messages are "
                    "waiting and will be handled first."
                )
            elif self.state.resident_quiescing or self.state.memory_maintenance:
                busy_reply = (
                    "Compaction is unavailable while the runtime is quiescing "
                    "or dreaming."
                )
            else:
                ticket = gate.claim(
                    RESIDENT_COMPACTION_SCOPE,
                    deadline_seconds=self.turn_executor.compaction.deadline_seconds(),
                    trigger=CompactionTrigger.MANUAL,
                )
                if ticket is None:
                    busy_reply = "A context compaction is already in progress."
        if ticket is None:
            await self._complete_compact_reply_async(
                action, busy_reply or "Compaction is unavailable."
            )
            return
        try:
            await self.control_runtime.notify("core.context_change.started", {"trigger": "manual"})
            memory_service = self.context.require_port(MEMORY)
            l1_items = list(
                memory_service.settled_transcripts() if memory_service is not None else ()
                or ()
            )
            if not l1_items:
                await self._complete_compact_reply_async(action, "Nothing to compact - memory is already minimal.")
                return
            run_result = await self._run_control_compaction_async(
                ticket,
            )
        finally:
            # Identity-checked: this ticket can never release a successor's
            # gate, and cancellation cannot skip this release (F07/Q14).
            async with self.state.channel_turn_transition_lock:
                gate.release(ticket)
            # Release follow-up (success or failure): queued input resumes
            # under whichever context is now authoritative (Q05/Q06).
            await self._start_next_queued_turn_async()
        if not run_result.success:
            message = f"Compaction did not complete ({run_result.status})."
            if run_result.status == "base_over_budget":
                # B05: the fixed base (system prompt + tools shell) exceeds
                # the context window on its own.  Compacting history cannot
                # fix that, and the base was not trimmed to fake a fit.
                message = (
                    "Compaction cannot help: the fixed base (system prompt "
                    "and tools) already exceeds the context window by "
                    "itself. Reduce the base or switch to a larger endpoint; "
                    "history was left unchanged."
                )
            if run_result.user_notice:
                message += "\n" + run_result.user_notice
            await self._complete_compact_reply_async(action, message)
            return
        result = run_result.memory_result
        reply_text = "Context compacted."
        if str(getattr(result, "summary", "") or "").strip():
            reply_text += " Continuity summary updated."
        normalization_diagnostics = compact_normalization_diagnostics(result)
        if normalization_diagnostics:
            reply_text += f" {len(normalization_diagnostics)} optional format issues normalized or skipped."
        if run_result.user_notice:
            reply_text += "\n" + run_result.user_notice
        await self._complete_compact_reply_async(
            action,
            reply_text,
        )
        await self._deliver_compact_candidates_async(action, result)

    async def _run_control_compaction_async(
        self, ticket: CompactionTicket, *, target_input_budget: int = 0,
        reserved_output_tokens: int = 0, preferred_endpoint_id: str | None = None,
        max_attempts: int | None = None, replay_guard: Callable[[], bool] | None = None,
        commit_guard: Callable[[], bool] | None = None,
        force_semantic: bool = False,
        keepalive_compact_epoch: str = "",
    ) -> CompactionRunResult:
        gate = self._compaction_gate()

        def eligible() -> bool:
            current = gate.ticket_for(ticket.scope)
            return (current is not None and current.op_id == ticket.op_id
                    and not current.cancelled and not current.expired
                    and (commit_guard is None or commit_guard()))
        async with self.state.channel_turn_transition_lock:
            if not eligible():
                return CompactionRunResult(status="ticket_cancelled")
            gate.advance(ticket, CompactionPhase.GENERATING)
        run_result = await self.turn_executor.compaction.run(
            self.context.require_port(MEMORY), target_input_budget=target_input_budget,
            reserved_output_tokens=reserved_output_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            max_attempts=max_attempts, replay_guard=replay_guard, commit_guard=eligible,
            **({"force_semantic": True} if force_semantic else {}),
            **({"keepalive_compact_epoch": keepalive_compact_epoch} if keepalive_compact_epoch else {}),
        )
        if run_result.success:
            async with self.state.channel_turn_transition_lock:
                gate.advance(ticket, CompactionPhase.COMMITTED)
        return run_result

    async def _deliver_compact_candidates_async(
        self, action: ControlAction, result: MemoryCompactResult,
    ) -> None:
        normalization_diagnostics = compact_normalization_diagnostics(result)
        memory_candidates = memory_candidates_from_compact_result(result)
        if memory_candidates:
            source_ref = f"compact_{uuid4().hex[:12]}"
            delivery = self.context.require_port(MEMORY).stage_memory_proposal(
                {
                    "source_kind": "pal_compact",
                    "source_ref": source_ref,
                    "source_label": "Pal compact",
                    "candidate_batch_id": source_ref,
                    "memory_candidates": memory_candidates,
                    "normalization_diagnostics": normalization_diagnostics,
                },
                action.route,
            )
            if delivery is not None:
                await self._deliver_control_delivery_async(delivery, fallback_route=action.route)

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
        await self._complete_action_reply_async(action, result.user_text)

    async def _render_reset_prompt_async(self, request) -> None:
        request.expired_delivery = control_interactions.terminal_delivery_for_interaction(
            request.route, interaction_id=request.request_id, interaction_kind="reset_confirm",
            text="This reset request expired.", delivery_kind="interactive_expire")
        already_opened = bool(request.payload.get("opened"))
        request.payload["opened"] = True
        await self._deliver_control_delivery_async(
            core_interactions.reset_confirm_delivery(request, already_opened=already_opened)
        )

    async def _complete_compact_reply_async(self, action: ControlAction, text: str) -> None:
        await self._complete_action_reply_async(action, text)

    async def _complete_action_reply_async(self, action: ControlAction, text: str) -> None:
        await self._deliver_control_delivery_async(control_interactions.terminal_delivery_for_action(action, text))

    async def _flush_control_status_async(
        self,
        route: ControlRoute | None,
    ) -> None:
        if route is None:
            return
        channel_runtime = self.context.port_registry.get("channel:channel")
        flush_status = getattr(channel_runtime, "flush_endpoint_status", None)
        if not callable(flush_status):
            return
        flush_status(route.endpoint_id)
        # Async channel providers schedule transport work from the synchronous
        # outbox flush. Yield once so the consumed UI starts moving before the
        # potentially long compaction request is dispatched.
        await asyncio.sleep(0)

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
        wait: bool = False,
    ) -> bool:
        envelope = self._route_to_channel_envelope(
            route,
            require_provider=require_provider,
        )
        if envelope is None:
            return False
        channel_runtime = self.context.require_port("channel:channel")
        reply_id = channel_runtime.queue_reply(
            self._delivery_binding_for_envelope(envelope),
            text,
        )
        if wait:
            return bool(await channel_runtime.wait_for_reply(reply_id))
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

    async def _run_turn_continuation_async(
        self,
        continuation: TurnContinuation,
        *,
        notify_settled: bool = True,
    ) -> TurnOutcome:
        current: EffectResult | None = None
        try:
            assembly_context = PromptAssemblyContext(
                event=continuation.opening_event,
                core_mode=self.state.mode,
            )
            self.turn_executor._begin_l1_turn(continuation, assembly_context)
            if (continuation.opening_event is not None
                    and continuation.opening_event.event_kind == EventKind.USER_MESSAGE):
                await self.turn_executor._append_background_handoffs(continuation, assembly_context)
            while True:
                if continuation.stop_after_tool_batch and not continuation.accepted_tool_batch_open:
                    continuation.interrupted = True
                    continuation.interrupt_reason = "interrupted"
                    lease = self.state.scheduler.state.owner
                    if lease is not None and lease.turn_id == continuation.turn_id:
                        self.state.scheduler.transition(SchedulingEvent.CANCEL, lease=lease)
                    await self.turn_manager._close_l1_turn_async(continuation, state="interrupted", reason="interrupted")
                if continuation.interrupted:
                    raise asyncio.CancelledError(continuation.interrupt_reason or "interrupted")
                yielded = self.turn_manager.resume(continuation, current)
                if isinstance(yielded, TurnOutcome):
                    outcome = yielded
                    await self._schedule_post_turn_commit_async(
                        outcome,
                        event=continuation.opening_event,
                        delivery_binding=continuation.delivery_binding,
                        notify_settled=False,
                    )
                    await self._deliver_pending_compact_memory_candidates_async(continuation)
                    self.turn_executor.clear_execution_cursors(continuation)
                    if notify_settled:
                        await self._notify_turn_settled_async(
                            outcome,
                            event=continuation.opening_event,
                            delivery_binding=continuation.delivery_binding,
                        )
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
        batches = list(continuation.pending_compact_memory_candidate_batches)
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
        route = ControlRoute(
            endpoint_id=binding.endpoint.endpoint_id,
            channel_kind=binding.endpoint.channel_kind,
            reply_target=dict(binding.response_handle.reply_target),
            control_scope_key=binding.control_scope_key,
            correlation_id=binding.correlation_id,
        )
        for index, batch in enumerate(batches):
            candidates = coerce_memory_candidate_list(batch.get("memory_candidates") if isinstance(batch, dict) else None)
            if not candidates:
                continue
            source_ref = batch.get("candidate_batch_id") or "compact_" + content_hash({"turn": continuation.turn_id, "index": index, "batch": batch})[:24]
            delivery = self.context.require_port(MEMORY).stage_memory_proposal(
                {
                    "source_kind": str(batch.get("source_kind") or "pal_compact"),
                    "source_ref": source_ref,
                    "source_label": str(batch.get("source_label") or "Pal compact"),
                    "candidate_batch_id": source_ref,
                    "memory_candidates": candidates,
                    "normalization_diagnostics": batch.get("normalization_diagnostics", []),
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
        continuation.pending_compact_memory_candidate_batches.clear()


    def publish_module_capabilities(self, module_id: str) -> list[str]:
        return self.module_lifecycle.publish_module_capabilities(module_id)

    def withdraw_module_capabilities(self, module_id: str) -> list[str]:
        return self.module_lifecycle.withdraw_module_capabilities(module_id)

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

    async def _execute_turn_effect_async(self, continuation: TurnContinuation, effect: EffectRequest) -> EffectResult:
        return await self.turn_executor.execute_turn_effect_async(continuation, effect)

    def _build_llm_tool_contracts(self) -> list[dict[str, object]]:
        return self.tool_surface.build_llm_tool_contracts()

    async def handle_failure_async(
        self,
        signal: FailureSignal,
        *,
        origin: str,
        route: str | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> FailureHandlingResult:
        return await self.failure_recovery.current.handle_failure_async(
            signal,
            origin=origin,
            route=route,
            conversation_context=conversation_context,
        )

    def _render_failure_feedback_text(self, feedback: FailureUserFeedback) -> str:
        return render_failure_feedback_text(feedback)

    def _should_enter_failure_flow_for_tool_result(self, tool_result: ToolExecutionResult) -> bool:
        if tool_result.ok:
            return False
        return (tool_result.structured or {}).get("error_code") == "handler_exception"

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
        root = self.config.runtime_root
        if root is None:
            print(text)
            return
        log_path = pal_log_path(Path(root))
        append_prompt_debug_log(log_path, text)

    async def _schedule_post_turn_commit_async(
        self,
        outcome: TurnOutcome,
        *,
        event: EventEnvelope | None = None,
        delivery_binding: TurnDeliveryBinding | None = None,
        notify_settled: bool = True,
    ) -> None:
        result = await self.turn_executor.schedule_post_turn_commit_async(outcome)
        if (
            self.context.port_registry.get("memory:memory") is not None
            and (result is None or result.state != "settled")
        ):
            raise RuntimeError(
                "Pal L1 working-set settlement failed; refusing to complete "
                "the logical turn"
            )
        if notify_settled:
            await self._notify_turn_settled_async(
                outcome,
                event=event,
                delivery_binding=delivery_binding,
                settled_result=result,
            )

    async def _notify_turn_settled_async(
        self,
        outcome: TurnOutcome,
        *,
        event: EventEnvelope | None,
        delivery_binding: TurnDeliveryBinding | None,
        settled_result: Any | None = None,
        notify_observers: bool = True,
    ) -> None:
        if (
            settled_result is not None
            and settled_result.state != "settled"
        ):
            return
        if (
            event is not None
            and str(event.event_kind or "") == EventKind.USER_MESSAGE
            and str(event.source_kind or "") == SourceKind.CHANNEL
        ):
            self.state.compaction_user_turn_count += 1
        if notify_observers and delivery_binding is not None:
            await self.control_runtime.notify("core.turn.settled", {
                "turn_id": outcome.commit_payload.turn_id,
                "event_kind": event.event_kind if event is not None else "",
                "source_kind": event.source_kind if event is not None else "",
                "route": ControlRoute(
                    endpoint_id=delivery_binding.endpoint.endpoint_id,
                    channel_kind=delivery_binding.endpoint.channel_kind,
                    reply_target=dict(delivery_binding.response_handle.reply_target),
                    control_scope_key=delivery_binding.control_scope_key,
                    correlation_id=delivery_binding.correlation_id,
                ),
            })


def _last_arg_of_type(args: tuple[Any, ...], expected_type):
    for item in reversed(args):
        if isinstance(item, expected_type):
            return item
    return None
