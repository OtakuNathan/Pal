from __future__ import annotations

import asyncio
import inspect
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pal.channel.contracts import ChannelEnvelope
from pal.core.contracts import CoreRuntimeState
from pal.core.dispatcher import EventDispatcher
from pal.core.failure_orchestrator import FailureHandlingResult, FailureOrchestrator
from pal.core.main_context import MainContext
from pal.core.module_lifecycle import ModuleLifecycle
from pal.core.prompt_compiler import PromptCompiler
from pal.core.tool_stagnation import ToolStagnationGuardProcess
from pal.core.tool_surface import ToolSurface
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import EffectRequest, EffectResult, TurnContinuation, TurnOutcome, channel_turn_program, service_turn_program
from pal.core.module_registry import ModuleHandle
from pal.failure import FailureSignal, FailureUserFeedback
from pal.foundation import EventEnvelope
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.service import ServiceDefinition, ServiceTriggerEvent, build_service_trigger_input
from pal.shared import EventKind, SourceKind
from pal.shared import IntrospectionPort, PromptAssemblyContext, PromptFragment, RuntimeStatus


@dataclass
class TurnRunner:
    context: MainContext
    state: CoreRuntimeState

    def run_turn(self, envelope: EventEnvelope) -> None:
        self.state.active_turns[envelope.event_id] = envelope

@dataclass
class TurnManager:
    context: MainContext
    state: CoreRuntimeState
    guard: ToolStagnationGuardProcess = field(default_factory=ToolStagnationGuardProcess)

    def start(self, channel_envelope: ChannelEnvelope) -> TurnContinuation:
        # A turn becomes a resumable computation object as soon as it enters the
        # runtime. After this point PalCore drives it by sending back effect results.
        turn_id = channel_envelope.event.event_id
        continuation = TurnContinuation(
            turn_id=turn_id,
            channel_envelope=channel_envelope,
            program=channel_turn_program(channel_envelope, core_mode=self.state.mode),
            correlation_id=channel_envelope.event.correlation_id or turn_id,
        )
        self.state.active_turns[turn_id] = continuation
        return continuation

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
            return outcome


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
                break
            processed.append(envelope)
        return processed


EventLoop = MainLoop


@dataclass
class PalCore:
    context: MainContext = field(default_factory=MainContext)
    state: CoreRuntimeState = field(default_factory=CoreRuntimeState)
    main_loop: MainLoop = field(default_factory=MainLoop)
    debug_prompt: bool = False
    turn_manager: TurnManager = field(init=False)
    prompt_compiler: PromptCompiler = field(init=False)
    tool_surface: ToolSurface = field(init=False)
    failure_orchestrator: FailureOrchestrator = field(init=False)
    turn_executor: TurnExecutor = field(init=False)
    module_lifecycle: ModuleLifecycle = field(init=False)

    def __post_init__(self) -> None:
        self.turn_manager = TurnManager(context=self.context, state=self.state)
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

    def process_channel_turn(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
        return asyncio.run(self.process_channel_turn_async(channel_envelope))

    async def process_channel_turn_async(self, channel_envelope: ChannelEnvelope) -> TurnOutcome:
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
                reply_envelope=self._resolve_service_reply_envelope(service_event, definition, trigger),
            ),
            correlation_id=service_event.correlation_id or service_event.event_id,
        )
        self.state.active_turns[continuation.turn_id] = continuation
        return await self._run_turn_continuation_async(continuation)

    async def _run_turn_continuation_async(self, continuation: TurnContinuation) -> TurnOutcome:
        current: EffectResult | None = None
        while True:
            yielded = self.turn_manager.resume(continuation, current)
            if isinstance(yielded, TurnOutcome):
                await self._schedule_post_turn_commit_async(yielded)
                return yielded
            current = await self._execute_turn_effect_async(continuation, yielded)

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
        prompt_ir = request.metadata.get("prompt_ir")
        system_parts = [message["content"] for message in request.messages if message.get("role") == "system"]
        user_parts = [message["content"] for message in request.messages if message.get("role") == "user"]
        print("=== PAL PROMPT DEBUG ===")
        print(f"sections={request.metadata.get('fragment_sections', [])}")
        if prompt_ir:
            print("--- prompt ir ---")
            print(prompt_ir)
        if system_parts:
            print("--- system ---")
            print(system_parts[0])
        if user_parts:
            print("--- user messages ---")
            for index, item in enumerate(user_parts):
                print(f"[{index}] {item}")
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
