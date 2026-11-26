"""Core-owned facilities shared by all local control requests."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar

from pal.control.contracts import ControlAction, ControlDelivery, ControlRoute
from pal.control.runtime_contracts import ControlBusyError, ControlRuntimeSnapshot
from pal.shared.timers import TimerScopePort
from pal.shared.maintenance import MaintenanceToken
from pal.core.compaction_coordinator import CompactionTicket
from pal.core.control_requests import ControlRequests
from pal.core.scheduling import ResidentTurnKind
from pal.foundation.diagnostics import exception_reason, exception_report
from pal.llm.conversions import tool_definition_ir_from_dict
from pal.llm.ir import LLMRequestIR, PromptRegionIR
from pal.llm.contracts import LLM_RUNTIME, collect_generation
from pal.shared.proactive_protocol import SILENT_ONCE_SCHEDULER
from pal.shared.tool_protocol import ToolExecutionResult
from pal.core.core_events import TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, TURN_TOOL_CALL_FAILED
from pal.memory.context_view import projected_messages
from pal.memory.prompt_context import prompt_date_pin
from pal.memory.contracts import MEMORY, MemoryCompactResult
from pal.shared import PromptAssemblyContext, TurnDeliveryBinding

if TYPE_CHECKING:
    from pal.core.module_registry import ModuleHandle
    from pal.core.runtime import PalCore
    from pal.core.turns import TurnContinuation, TurnOutcome
    from pal.llm.contracts import LLMGenerationResult
    from pal.shared.tool_protocol import ToolCallIR
    from pal.llm.compaction_engine import CompactionRunResult

T = TypeVar("T")


class ContextChange:
    def __init__(self, core: PalCore, ticket: CompactionTicket) -> None:
        self._core, self._ticket = core, ticket

    def current(self) -> bool:
        held = self._core._compaction_gate().ticket_for(self._ticket.scope)
        return (held is not None and held.op_id == self._ticket.op_id
                and not held.cancelled and not held.expired)

    async def commit(self, operation: Callable[[], None], *, guard: Callable[[], bool]) -> None:
        async with self._core.state.channel_turn_transition_lock:
            if not self.current() or not guard():
                raise ValueError("The context change was cancelled or its configuration changed.")
            operation()

    async def compact(
        self, *, preferred_endpoint_id: str | None = None, target_input_budget: int = 0,
        reserved_output_tokens: int = 0, guard: Callable[[], bool] | None = None,
        max_attempts: int | None = None, replay_guard: Callable[[], bool] | None = None,
        force_semantic: bool = False,
    ) -> CompactionRunResult:
        await self._core.control_runtime.notify("core.context_change.started", {"trigger": self._ticket.trigger})
        return await self._core._run_control_compaction_async(
            self._ticket, preferred_endpoint_id=preferred_endpoint_id,
            target_input_budget=target_input_budget, reserved_output_tokens=reserved_output_tokens,
            commit_guard=guard, max_attempts=max_attempts, replay_guard=replay_guard, force_semantic=force_semantic)

    async def close(self) -> None:
        async with self._core.state.channel_turn_transition_lock:
            self._core._compaction_gate().release(self._ticket)
        await self._core._start_next_queued_turn_async()


class CoreControlRuntime:
    def __init__(self, core: PalCore) -> None:
        self._core = core
        self.requests = ControlRequests(core)

    def notify_ready(self) -> None:
        self._core.notify_ready()

    def record_diagnostic(self, kind: str, payload: dict[str, Any]) -> None:
        self._core.state.diagnostics.append({**payload, "kind": kind})

    def retain_modules(self, module_ids: tuple[str, ...]) -> Callable[[], None]:
        leases = []
        try:
            for name in dict.fromkeys(module_ids):
                handle = self._core.context.module_registry.require(name)
                if not self._core.context.module_registry.is_current(handle):
                    raise RuntimeError(f"Module is no longer mounted: {name}")
                lease = handle.background_work.acquire()
                if lease is None:
                    raise RuntimeError(f"Module is retiring: {name}")
                leases.append(lease)
        except BaseException:
            for lease in leases:
                lease.close()
            raise
        def release() -> None:
            for lease in leases:
                lease.close()
        return release

    def idle_module_operation(self, module_id: str, operation: Callable[[], T]) -> T:
        handle = self._core.context.module_registry.require(module_id)
        if not self._core.context.module_registry.is_current(handle):
            raise RuntimeError(f"Module is no longer mounted: {module_id}")
        handle.background_work.retire()
        try:
            return operation()
        finally:
            handle.background_work.resume()

    async def idle_operation(self, operation: Callable[[], T]) -> T:
        async with self._core.state.channel_turn_transition_lock:
            if self._core.memory_maintenance_busy():
                raise ControlBusyError("Pal is busy; retry when idle")
            return operation()

    def can_start_background_turn(self, *, kind: ResidentTurnKind = ResidentTurnKind.PROACTIVE) -> bool:
        return not self._core.resident_admission_reason(kind)

    def delivery_binding_for_turn(self, turn_id: str | None) -> TurnDeliveryBinding | None:
        continuation = self._core.state.active_turns.get(str(turn_id or ""))
        return continuation.delivery_binding if continuation is not None else None

    def turn_execution_options(self) -> dict[str, Any]:
        return self._core.turn_execution_options()

    def start_background_turn(self, continuation: TurnContinuation,
                              runner: Callable[[], Awaitable[str]], *,
                              owner: ModuleHandle | None,
                              kind: ResidentTurnKind = ResidentTurnKind.PROACTIVE) -> asyncio.Task[str] | None:
        return self._core.start_background_turn(continuation, runner, owner=owner, kind=kind)

    async def run_turn(self, continuation: TurnContinuation) -> TurnOutcome:
        return await self._core.run_turn_continuation_async(continuation)

    def finish_interrupted_turn(self, turn_id: str, *, reason: str) -> None:
        self._core.turn_manager.cleanup_interrupted(turn_id, reason=reason)

    async def generate(self, request: LLMRequestIR) -> LLMGenerationResult:
        parent = self._core.state.active_turns.get(str(request.metadata.get("parent_turn_id") or ""))
        if parent is not None and parent.stop_after_tool_batch:
            raise RuntimeError("Recovery generation stopped by resident interrupt")
        self._core._debug_log_prompt(request)
        generation = collect_generation(self._core.context.require_port(LLM_RUNTIME).generate(request))
        if parent is None or not parent.accepted_tool_batch_open:
            return await generation
        task = asyncio.create_task(generation)
        parent.recovery_generation_task = task
        try:
            return await task
        except asyncio.CancelledError:
            running = asyncio.current_task()
            if parent.stop_after_tool_batch and not parent.interrupted and (running is None or not running.cancelling()):
                raise RuntimeError("Recovery generation stopped by resident interrupt") from None
            raise
        finally:
            if parent.recovery_generation_task is task:
                parent.recovery_generation_task = None

    async def execute_local_tool(self, call: ToolCallIR, *, scope_id: str, phase: str,
                                 turn_id: str = "") -> ToolExecutionResult:
        parent = self._core.state.active_turns.get(turn_id)
        if phase == "safe_mode" and parent is not None and parent.stop_after_tool_batch:
            return ToolExecutionResult(name=call.name, ok=False, call_id=call.call_id,
                                       llm_text="Optional recovery stopped by resident interrupt")
        event = {"subsystem": "execution", "component": call.args.get("name", call.name)
                 if call.name == "call_tool" else call.name, "tool_name": call.name,
                 "call_id": call.call_id, "scope_id": scope_id, "phase": phase}
        if turn_id:
            event["turn_id"] = turn_id
        bus, execution = self._core.context.core_event_bus, self._core.context.execution_runtime
        bus.emit(TURN_TOOL_CALL_BEFORE, event)
        try:
            result = await execution.execute_tool_async(call, allow_tools=True)
        except Exception as exc:
            result = execution.tool_call_failed_result(call, exc)
        if not result.ok:
            bus.emit(TURN_TOOL_CALL_FAILED, {**event, "ok": False})
        bus.emit(TURN_TOOL_CALL_AFTER, {**event, "ok": result.ok})
        return result

    def ensure_silent_once(self, *, proactive_id: str, goal: str, method: str) -> str | None:
        scheduler = self._core.context.port_registry.optional(SILENT_ONCE_SCHEDULER)
        return scheduler.ensure_silent_once(proactive_id=proactive_id, goal=goal, method=method) if scheduler is not None else None

    async def try_enter_memory_maintenance_async(self) -> MaintenanceToken | None:
        return await self._core.try_enter_memory_maintenance_async()

    async def wait_for_memory_maintenance_async(self) -> None:
        await self._core.wait_for_memory_maintenance_async()

    def begin_memory_sleep(self, token: MaintenanceToken, *, component: str = "maintenance") -> None:
        self._core.begin_memory_sleep(token, component=component)

    async def leave_memory_maintenance_async(self, token: MaintenanceToken) -> bool:
        return bool(await self._core.leave_memory_maintenance_async(token))

    def snapshot(self) -> ControlRuntimeSnapshot:
        state = self._core.state
        return ControlRuntimeSnapshot(
            active_turns=len(state.active_turns), queued_inputs=len(state.pending_channel_turns),
            quiescing=state.resident_quiescing, memory_maintenance=state.memory_maintenance,
            prompt_log_enabled=state.prompt_log_enabled,
            memory_maintenance_component=state.memory_maintenance_component,
            memory_maintenance_token=state.memory_maintenance_token)

    async def reply(self, action: ControlAction, text: str) -> None:
        await self._core._complete_action_reply_async(action, text)

    def last_user_route(self) -> ControlRoute | None:
        return self._core.memory_notification_route()

    async def deliver(self, delivery: ControlDelivery, *, require_provider: bool = False, wait: bool = False) -> bool:
        if wait and delivery.delivery_kind == "reply":
            return bool(await self._core._reply_to_route_async(
                delivery.route, delivery.text, require_provider=require_provider, wait=True))
        return bool(await self._core._deliver_control_delivery_async(delivery, require_provider=require_provider))

    async def flush(self, route: ControlRoute | None) -> None:
        await self._core._flush_control_status_async(route)

    async def publish_catalog(self, *, endpoint_id: str | None = None) -> None:
        await self._core.publish_control_catalog_async(endpoint_id=endpoint_id)

    def capture_prompt(self) -> LLMRequestIR:
        core = self._core
        memory = core.context.require_port(MEMORY)
        pin = prompt_date_pin(memory)
        prompt = core.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(metadata={"typed_l1_projection": True,
                                            **({"prompt_utc_date": pin["date"]} if pin else {})}),
            max_output_tokens=core.config.fallback_max_output_tokens)
        history = memory.project_continuity([
            message for turn in memory.history.turns for message in projected_messages(turn, settled=True)])
        return replace(prompt,
            messages=(*prompt.messages, *(replace(m, prompt_region=PromptRegionIR.SETTLED_HISTORY) for m in history)),
            tools=tuple(tool_definition_ir_from_dict(tool) for tool in core._build_llm_tool_contracts()))

    def busy_reason(self) -> str:
        core = self._core
        if (core.turn_manager.latest_active_turn_id() is not None
                or any(not task.done() for task in core.state.turn_tasks.values())):
            return "a turn is active or finishing"
        if core.state.pending_channel_turns:
            return "earlier messages are waiting"
        if core.state.resident_quiescing or core.state.memory_maintenance or core._compaction_gate_active():
            return "compaction or maintenance is in progress"
        return ""

    async def begin_context_change(
        self, trigger: str, *, claim: Callable[[], None] | None = None,
        max_attempts: int | None = None,
    ) -> ContextChange:
        async with self._core.state.channel_turn_transition_lock:
            reason = self.busy_reason()
            if reason:
                raise ControlBusyError(reason)
            if not trigger.strip():
                raise ValueError("context change trigger is required")
            deadline = self._core.turn_executor.compaction.deadline_seconds(max_attempts=max_attempts)
            if claim is not None:
                claim()
            ticket = self._core._compaction_gate().claim(
                "pal:resident", trigger=trigger, deadline_seconds=deadline)
        if ticket is None:
            raise ControlBusyError("another context change is in progress")
        return ContextChange(self._core, ticket)

    async def cancel_context_change(self, *, reason: str) -> None:
        async with self._core.state.channel_turn_transition_lock:
            self._core._compaction_gate().cancel("pal:resident", reason=reason)
            self._core.context.require_port(MEMORY).cancel_active_compaction(reason=reason)

    async def configuration_change(self, operation: Callable[[], T], *, reason: str) -> T:
        async with self._core.state.channel_turn_transition_lock:
            self.requests.invalidate_context()
            self._core._compaction_gate().cancel_all(reason=reason)
            return operation()

    def open_timer_scope(self) -> TimerScopePort:
        return self._core.main_loop.timers.open_scope()

    async def user_activity(self) -> None:
        await self.notify("core.user_activity", {})

    async def deliver_compact_candidates(self, action: ControlAction, result: MemoryCompactResult) -> None:
        await self._core._deliver_compact_candidates_async(action, result)

    async def notify(self, topic: str, payload: dict[str, Any]) -> dict[str, str]:
        errors = await self._core.context.control_action_registry.notify(topic, payload)
        for owner, exc in errors.items():
            self._core.state.diagnostics.append({
                "kind": "control.observer.failed", "topic": topic,
                "module": owner, "error": exception_report(exc),
            })
        return {owner: exception_reason(exc) for owner, exc in errors.items()}
