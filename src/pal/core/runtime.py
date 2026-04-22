from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pal.channel.contracts import ChannelEnvelope
from pal.control.contracts import ControlAction, ControlRoute
from pal.control.routing import derive_control_scope_key
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
from pal.core.turns import EffectRequest, EffectResult, TurnContinuation, TurnOutcome, L1CommitPayload, channel_turn_program, service_turn_program
from pal.core.module_registry import ModuleHandle
from pal.execution import CapabilityCall
from pal.foundation import EventEnvelope, utc_now
from pal.failure import FailureSignal, FailureUserFeedback
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.service import ServiceDefinition, ServiceTriggerEvent, build_service_trigger_input
from pal.shared import EventKind, SourceKind
from pal.shared import IntrospectionPort, PromptAssemblyContext, PromptFragment, RuntimeStatus


@dataclass
class TurnRunner:
    context: MainContext
    state: CoreRuntimeState

    def run_turn(self, envelope: EventEnvelope) -> None:
        _ = (self.context, self.state, envelope)

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
        scope_state.active_turn_id = turn_id
        scope_state.drained_event.clear()
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
            turn_id = scope_state.active_turn_id
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
            channel_runtime = self.context.port_registry.get("channel:channel")
            if channel_runtime is not None and isinstance(continuation, TurnContinuation):
                channel_runtime.abort_stream(continuation.channel_envelope.response_handle, reason=reason)
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
        return {"think_level": think_level or "balanced"}

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

    def enqueue(self, envelope: EventEnvelope) -> None:
        self.queue.append(envelope)

    def pop(self) -> EventEnvelope | None:
        if not self.queue:
            return None
        return self.queue.popleft()

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
        TurnRunner(context=context, state=state).run_turn(envelope)
        for derived in await self.dispatcher.dispatch_async(envelope, context):
            self.enqueue(derived)
        return envelope

    def run_until_idle(self, context: MainContext, state: CoreRuntimeState, *, max_iterations: int = 64) -> list[EventEnvelope]:
        processed: list[EventEnvelope] = []
        for _ in range(max_iterations):
            envelope = self.run_once(context, state)
            if envelope is None:
                break
            processed.append(envelope)
        return processed

    async def run_until_idle_async(self, context: MainContext, state: CoreRuntimeState, *, max_iterations: int = 64) -> list[EventEnvelope]:
        processed: list[EventEnvelope] = []
        for _ in range(max_iterations):
            envelope = await self.run_once_async(context, state)
            if envelope is None:
                pending = [task for task in state.turn_tasks.values() if task is not None and not task.done()]
                if not pending:
                    break
                done, _ = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    continue
                continue
            processed.append(envelope)
        return processed


EventLoop = MainLoop


@dataclass
class PalCore:
    context: MainContext = field(default_factory=MainContext)
    state: CoreRuntimeState = field(default_factory=CoreRuntimeState)
    config: RuntimeConfig = field(default_factory=RuntimeConfig.defaults)
    main_loop: MainLoop = field(default_factory=MainLoop)
    debug_prompt: bool = False
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

    @property
    def event_loop(self) -> MainLoop:
        return self.main_loop

    def receive_event(self, envelope: EventEnvelope) -> None:
        self.main_loop.enqueue(envelope)

    def drain_once(self) -> EventEnvelope | None:
        return asyncio.run(self.drain_once_async())

    async def drain_once_async(self) -> EventEnvelope | None:
        return await self.main_loop.run_once_async(self.context, self.state)

    def run_until_idle(self, *, max_iterations: int = 64) -> list[EventEnvelope]:
        return asyncio.run(self.run_until_idle_async(max_iterations=max_iterations))

    async def run_until_idle_async(self, *, max_iterations: int = 64) -> list[EventEnvelope]:
        return await self.main_loop.run_until_idle_async(self.context, self.state, max_iterations=max_iterations)

    async def schedule_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> None:
        control_scope_key = derive_control_scope_key(
            endpoint_id=channel_envelope.endpoint.endpoint_id,
            channel_kind=channel_envelope.endpoint.channel_kind,
            reply_target=channel_envelope.response_handle.reply_target,
            payload=channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {},
        )
        scope_state = self._ensure_scope_state(control_scope_key)
        if scope_state.quiescing:
            await self._reply_to_route_async(
                self._route_from_channel_envelope(channel_envelope),
                "This scope is resetting. Please retry in a moment.",
            )
            return
        turn_id = channel_envelope.event.event_id
        if turn_id in self.state.turn_tasks and not self.state.turn_tasks[turn_id].done():
            return
        task = asyncio.create_task(self._background_channel_turn_runner_async(channel_envelope))
        self.state.turn_tasks[turn_id] = task
        task.add_done_callback(lambda finished, current_turn_id=turn_id: self._on_turn_task_done(current_turn_id, finished))

    def process_channel_turn(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        return asyncio.run(self.process_channel_turn_async(channel_envelope))

    async def process_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        control_scope_key = derive_control_scope_key(
            endpoint_id=channel_envelope.endpoint.endpoint_id,
            channel_kind=channel_envelope.endpoint.channel_kind,
            reply_target=channel_envelope.response_handle.reply_target,
            payload=channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {},
        )
        scope_state = self._ensure_scope_state(control_scope_key)
        if scope_state.quiescing:
            raise RuntimeError("scope is quiescing")
        # The hot path is: start turn -> interpret yielded effects -> resume
        # until the generator returns a TurnOutcome.
        continuation = self.turn_manager.start(channel_envelope)
        return await self._run_turn_continuation_async(continuation)

    async def process_service_trigger_async(
        self,
        trigger: ServiceTriggerEvent,
        definition: ServiceDefinition,
    ) -> TurnOutcome:
        service_input = build_service_trigger_input(definition)
        service_event = EventEnvelope(
            event_kind=EventKind.SERVICE_TRIGGER,
            source_kind=SourceKind.SERVICE,
            payload={
                "text": service_input,
                "service_id": definition.service_id,
                "trigger_kind": trigger.trigger_kind,
                "metadata": dict(trigger.metadata or {}),
            },
            correlation_id=str(trigger.metadata.get("request_id") or trigger.service_id),
        )
        trigger_metadata = dict(trigger.metadata or {})
        trigger_metadata.setdefault("turn_id", service_event.event_id)
        trigger = ServiceTriggerEvent(
            service_id=trigger.service_id,
            trigger_kind=trigger.trigger_kind,
            metadata=trigger_metadata,
        )
        synthetic_envelope = ChannelEnvelope(
            event=service_event,
            endpoint=self._build_service_endpoint_config(definition),
            response_handle=self._build_service_response_handle(definition),
        )
        continuation = TurnContinuation(
            turn_id=service_event.event_id,
            channel_envelope=synthetic_envelope,
            program=service_turn_program(
                trigger,
                definition,
                core_mode=self.state.mode,
                max_output_tokens=self.turn_manager._resolve_max_output_tokens(),
                reply_envelope=self._resolve_service_reply_envelope(service_event, definition, trigger),
            ),
            correlation_id=service_event.correlation_id or service_event.event_id,
        )
        self.state.active_turns[continuation.turn_id] = continuation
        return await self._run_turn_continuation_async(continuation)

    async def _background_channel_turn_runner_async(self, channel_envelope: ChannelEnvelope) -> None:
        turn_id = channel_envelope.event.event_id
        try:
            await self.process_channel_turn_async(channel_envelope)
        except asyncio.CancelledError:
            self.turn_manager.cleanup_interrupted(turn_id, reason="interrupted")
        except Exception as exc:
            self.turn_manager.cleanup_interrupted(turn_id, reason="failed")
            self.state.diagnostics.append(
                {
                    "kind": "turn.background.failed",
                    "turn_id": turn_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    def _on_turn_task_done(self, turn_id: str, task: asyncio.Task[Any]) -> None:
        stored = self.state.turn_tasks.get(turn_id)
        if stored is task:
            self.state.turn_tasks.pop(turn_id, None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    async def handle_control_action_async(self, action: ControlAction) -> None:
        await self.expire_pending_control_requests_async()
        if action.action_kind == "show_panel":
            await self._handle_show_panel_async(action)
            return
        if action.action_kind == "show_think":
            await self._handle_show_think_async(action)
            return
        if action.action_kind == "set_think":
            await self._handle_set_think_async(action)
            return
        if action.action_kind == "interrupt_turn":
            await self._handle_interrupt_turn_async(action)
            return
        if action.action_kind == "open_reset_confirm":
            await self._handle_open_reset_confirm_async(action)
            return
        if action.action_kind == "reset_memory":
            await self._handle_reset_memory_async(action)
            return
        if action.action_kind == "invoke_capability":
            await self._handle_invoke_capability_async(action)
            return
        if action.action_kind == "invalid_command":
            await self._reply_to_route_async(action.route, action.notes or "Invalid command.")
            return
        if action.action_kind == "unknown_command":
            command_name = str(action.args.get("command_name") or "").strip()
            text = f"Unknown command: /{command_name}" if command_name else "Unknown command."
            await self._reply_to_route_async(action.route, text)
            return
        await self._reply_to_route_async(
            action.route,
            f"Control action '{action.action_kind}' is not wired yet.",
        )

    async def publish_control_catalog_async(self, *, endpoint_id: str | None = None) -> None:
        control_plane = self.context.port_registry.get("control:control")
        channel_runtime = self.context.port_registry.get("channel:channel")
        if control_plane is None or channel_runtime is None:
            return
        payload = self._build_control_catalog_payload(control_plane)
        queue_endpoint_status = getattr(channel_runtime, "queue_endpoint_status", None)
        if not callable(queue_endpoint_status):
            return
        if endpoint_id:
            queue_endpoint_status(endpoint_id, "control_catalog", payload=payload)
            return
        list_endpoints = getattr(channel_runtime, "list_endpoints", None)
        if not callable(list_endpoints):
            return
        for endpoint in list_endpoints():
            queue_endpoint_status(endpoint.endpoint.endpoint_id, "control_catalog", payload=payload)

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
        text = control_plane.render_panel_text()
        panel_payload = self._build_control_panel_payload(control_plane)
        if action.route is not None and action.route.channel_kind == "telegram":
            await self.publish_control_catalog_async(endpoint_id=action.route.endpoint_id)
            await self._status_to_route_async(action.route, "control_panel", panel_payload)
            return
        await self._reply_to_route_async(action.route, text)

    async def _handle_show_think_async(self, action: ControlAction) -> None:
        llm_runtime = self.context.require_port("llm:llm")
        refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
        if callable(refresh):
            refresh()
        think_level = str(getattr(llm_runtime, "think_level", "balanced") or "balanced")
        await self._reply_to_route_async(action.route, f"Current think level: {think_level}")

    async def _handle_set_think_async(self, action: ControlAction) -> None:
        requested = str(action.args.get("think_level") or "").strip() or "balanced"
        llm_runtime = self.context.require_port("llm:llm")
        settings_repository = getattr(llm_runtime, "settings_repository", None)
        if settings_repository is not None:
            settings_repository.set_think_level(requested)
        refresh = getattr(llm_runtime, "refresh_runtime_settings", None)
        if callable(refresh):
            refresh()
        await self._reply_to_route_async(
            action.route,
            f"Think level updated to {requested}. This applies to new turns only.",
        )

    async def _handle_interrupt_turn_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        interrupted = await self.turn_manager.interrupt_by_scope(route.control_scope_key, reason="interrupted")
        if not interrupted:
            await self._reply_to_route_async(route, "No active turn to interrupt in this scope.")
            return
        await self._reply_to_route_async(route, "Interrupted the current turn.")

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

    async def _handle_reset_memory_async(self, action: ControlAction) -> None:
        route = action.route
        if route is None:
            return
        request_id = str(action.args.get("request_id") or "").strip()
        scope_state = self._ensure_scope_state(route.control_scope_key)
        request = scope_state.pending_requests.get("reset_confirm")
        if request is None or request.request_id != request_id:
            await self._reply_to_route_async(route, "Reset request is missing, expired, or already consumed.")
            return
        if _parse_utc_timestamp(request.expires_at) <= datetime.now(timezone.utc):
            scope_state.pending_requests.pop("reset_confirm", None)
            await self._notify_expired_request_async(request)
            return
        await self._execute_soft_reset_async(scope_state, request)
        scope_state.pending_requests.pop("reset_confirm", None)
        await self._reply_to_route_async(route, "Soft reset complete. L1/L2 and working memory projection were cleared.")

    async def _handle_invoke_capability_async(self, action: ControlAction) -> None:
        capability_name = str(action.target_id or "").strip()
        if not capability_name:
            await self._reply_to_route_async(action.route, "Missing capability target.")
            return
        result = await self.context.execution_runtime.execute_async(
            CapabilityCall(name=capability_name, args=dict(action.args))
        )
        await self._reply_to_route_async(action.route, str(result.text or result.llm_text))

    async def _execute_soft_reset_async(self, scope_state, request) -> None:
        async with scope_state.transition_lock:
            if scope_state.quiescing:
                return
            scope_state.quiescing = True
            scope_state.drained_event = asyncio.Event()
            current_turn_id = scope_state.active_turn_id
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

    async def _render_reset_prompt_async(self, request) -> None:
        route = request.route
        text = (
            "Reset working memory for this scope?\n"
            "This clears L1, L2, and conversation-facing projection only.\n"
            "Durable L3 memory stays intact.\n"
            f"Confirm with /reset confirm {request.request_id}"
        )
        if route.channel_kind == "telegram":
            await self._status_to_route_async(
                route,
                "control_prompt",
                {
                    "text": text,
                    "request_id": request.request_id,
                    "confirm_command": f"/reset confirm {request.request_id}",
                    "cancel_command": "/control",
                    "prompt_kind": "reset_confirm",
                    "buttons": [
                        {"label": "Confirm Reset", "command": f"/reset confirm {request.request_id}"},
                        {"label": "Back", "command": "/control"},
                    ],
                },
            )
            return
        await self._reply_to_route_async(route, text)

    async def _notify_expired_request_async(self, request) -> None:
        text = "This reset request expired."
        if request.route.channel_kind == "telegram":
            await self._status_to_route_async(
                request.route,
                "control_request_expired",
                {
                    "request_id": request.request_id,
                    "text": text,
                },
            )
            return
        await self._reply_to_route_async(request.route, text)

    def _build_control_panel_payload(self, control_plane) -> dict[str, Any]:
        commands = control_plane.list_panel_commands()
        buttons: list[dict[str, str]] = []
        for spec in commands:
            if spec.name == "control":
                continue
            if spec.name == "think":
                buttons.extend(
                    [
                        {"label": "Think: Low", "command": "/think low"},
                        {"label": "Think: Balanced", "command": "/think balanced"},
                        {"label": "Think: Deep", "command": "/think deep"},
                    ]
                )
                continue
            if spec.name == "interrupt":
                buttons.append({"label": "Interrupt", "command": "/interrupt"})
                continue
            if spec.name == "reset":
                buttons.append({"label": "Reset Memory", "command": "/reset"})
                continue
            if spec.panel_button:
                buttons.append({"label": spec.name, "command": f"/{spec.name}"})
        return {
            "text": control_plane.render_panel_text(),
            "buttons": buttons,
        }

    def _build_control_catalog_payload(self, control_plane) -> dict[str, Any]:
        commands: list[dict[str, str]] = []
        for spec in control_plane.list_panel_commands():
            command = str(spec.name or "").strip().lower()
            description = str(spec.description or "").strip()
            if not command or not description:
                continue
            commands.append(
                {
                    "command": command,
                    "description": description,
                }
            )
        return {"commands": commands}

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
        payload = channel_envelope.event.payload if isinstance(channel_envelope.event.payload, dict) else {}
        return ControlRoute(
            endpoint_id=channel_envelope.endpoint.endpoint_id,
            channel_kind=channel_envelope.endpoint.channel_kind,
            reply_target=dict(channel_envelope.response_handle.reply_target),
            control_scope_key=derive_control_scope_key(
                endpoint_id=channel_envelope.endpoint.endpoint_id,
                channel_kind=channel_envelope.endpoint.channel_kind,
                reply_target=channel_envelope.response_handle.reply_target,
                payload=payload,
            ),
            correlation_id=channel_envelope.event.correlation_id or channel_envelope.event.event_id,
        )

    def _route_to_channel_envelope(self, route: ControlRoute | None) -> ChannelEnvelope | None:
        if route is None:
            return None
        channel_runtime = self.context.port_registry.get("channel:channel")
        endpoint = channel_runtime.get_endpoint(route.endpoint_id) if channel_runtime is not None else None
        endpoint_config = endpoint.endpoint if endpoint is not None else None
        if endpoint_config is None:
            from pal.channel.contracts import EndpointConfig, ResponseHandle

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
        while True:
            if continuation.interrupted:
                raise asyncio.CancelledError(continuation.interrupt_reason or "interrupted")
            yielded = self.turn_manager.resume(continuation, current)
            if isinstance(yielded, TurnOutcome):
                outcome = self._enrich_transcript_with_tool_protocol(yielded, continuation)
                await self._schedule_post_turn_commit_async(outcome)
                return outcome
            current = await self._execute_turn_effect_async(continuation, yielded)

    _TOOL_RESULT_PREVIEW_CHARS = 600
    _TOOL_RESULT_L1_BUDGET = 2000

    def _truncate_tool_result_for_l1(self, content: str) -> str:
        if len(content) <= self._TOOL_RESULT_L1_BUDGET:
            return content
        preview = content[:self._TOOL_RESULT_PREVIEW_CHARS].rstrip()
        return f"{preview}\n\n[... truncated, original: {len(content)} chars]"

    def _enrich_transcript_with_tool_protocol(self, outcome: TurnOutcome, continuation: TurnContinuation) -> TurnOutcome:
        from pal.memory.contracts import L1TranscriptMessage

        if not continuation.tool_protocol_messages:
            return outcome
        original = outcome.commit_payload.transcript
        user_msg = next((m for m in original if m.role == "user"), None)
        new_transcript: list[L1TranscriptMessage] = []
        if user_msg:
            new_transcript.append(user_msg)
        for msg in continuation.tool_protocol_messages:
            content = str(msg.get("content", ""))
            if msg.get("role") == "tool":
                content = self._truncate_tool_result_for_l1(content)
            new_transcript.append(L1TranscriptMessage(
                role=str(msg.get("role", "")),
                content=content,
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            ))
        assistant_msgs = [m for m in original if m.role == "assistant"]
        for m in assistant_msgs:
            new_transcript.append(L1TranscriptMessage(role=m.role, content=m.content))
        return TurnOutcome(
            turn_id=outcome.turn_id,
            final_reply=outcome.final_reply,
            commit_payload=L1CommitPayload(
                turn_id=outcome.commit_payload.turn_id,
                transcript=new_transcript,
                tool_observations=outcome.commit_payload.tool_observations,
            ),
        )

    def _build_service_endpoint_config(self, definition: ServiceDefinition):
        from pal.channel.contracts import EndpointConfig

        return EndpointConfig(
            endpoint_id=f"service:{definition.service_id}",
            channel_kind=SourceKind.SERVICE,
            binding_key=definition.service_id,
        )

    def _build_service_response_handle(self, definition: ServiceDefinition):
        from pal.channel.contracts import ResponseHandle

        return ResponseHandle(endpoint_id=f"service:{definition.service_id}")

    def _resolve_service_reply_envelope(
        self,
        service_event: EventEnvelope,
        definition: ServiceDefinition,
        trigger: ServiceTriggerEvent,
    ) -> ChannelEnvelope | None:
        out_channel_id = str(definition.out_channel_id or "").strip()
        if not out_channel_id:
            return None
        channel_runtime = self.context.port_registry.get("channel:channel")
        if channel_runtime is None:
            return None
        endpoint_runtime = channel_runtime.get_endpoint(out_channel_id)
        if endpoint_runtime is None:
            return None
        reply_target = endpoint_runtime.derive_default_reply_target()
        reply_target.update(dict(definition.out_reply_target or {}))
        reply_target.update(dict(trigger.metadata.get("reply_target") or {}))
        if endpoint_runtime.endpoint.channel_kind == "socket" and not reply_target:
            return None
        return ChannelEnvelope(
            event=service_event,
            endpoint=endpoint_runtime.endpoint,
            response_handle=endpoint_runtime.build_response_handle(reply_target=reply_target),
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

    def _debug_log_prompt(self, request: CanonicalLLMRequest) -> None:
        if not self.debug_prompt:
            return
        print("=== PAL PROMPT DEBUG ===")
        print("--- request.messages ---")
        print(request.messages)
        print("--- request.tools ---")
        print(request.tools)
        print("=== END PAL PROMPT DEBUG ===")

    def _debug_log_outcome(self, outcome: CanonicalLLMOutcome) -> None:
        if not self.debug_prompt:
            return
        print("=== PAL LLM OUTCOME ===")
        print(f"finish_reason: {outcome.finish_reason}")
        print(f"response_mode: {outcome.response_mode}")
        print(f"tool_calls: {outcome.tool_calls}")
        print(f"reasoning_text (first 500): {str(outcome.reasoning_text or '')[:500]}")
        print(f"text (first 2000): {str(outcome.text or '')[:2000]}")
        print("=== END PAL LLM OUTCOME ===")

    def _debug_log_reply(self, text: str) -> None:
        if not self.debug_prompt:
            return
        print("=== PAL TG REPLY ===")
        print(text)
        print("=== END PAL TG REPLY ===")

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
