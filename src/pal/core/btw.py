"""Slash-command side turns: borrowed context, independent transient ownership."""
from __future__ import annotations

import asyncio
from copy import copy, deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

from pal.control.contracts import ControlAction
from pal.core.agent_turn_runtime import AgentTurnRuntime
from pal.core.core_events import TURN_END, TURN_START
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import (
    EffectResult, LLMPreflightEffect, LLMRequestEffect, MemoryCompactEffect,
    TurnContinuation, TurnOutcome, channel_turn_program,
)
from pal.llm.contracts import LLMGenerationResult, LLMPreflightRequest, LLM_SIDE_TURNS
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory.persistence.contracts import RESIDENT_HISTORY
from pal.memory.service import InMemoryL1Store, MemoryService
from pal.memory.turn_ir import L1TurnState, L1TurnStore
from pal.shared import EventKind, LLMFinishReason, LLMPreflightStatus, RuntimeStatus
from pal.shared.ports import PortRegistry


BTW_INSTRUCTION = (
    "This is a one-shot side question, not a continuing conversation. "
    "Prefer answering directly from the available context. "
    "If tools are needed, perform read-only operations only. Do not modify files, configuration "
    "or runtime state, send messages, or perform any other operation with write side effects. "
    "Do not start background tasks, long-lived processes, or shell sessions requiring later management. "
    "If a shell query is needed, finish it within this request and leave no session behind. "
    "There is no handoff of side-question sessions to the main conversation. "
    "If a change is needed, explain the recommendation and leave execution to the main conversation."
)


class BtwTurnExecutor(TurnExecutor):
    """Use ordinary generation/tool handling with a temporary history port."""

    include_live_execution_context = False
    failed = False

    def build_turn_prompt(self, *args, **kwargs):
        prompt = super().build_turn_prompt(*args, **kwargs)
        return replace(prompt, metadata={**dict(prompt.metadata), "transient_history": True})

    def _history_submission_observer(self, request):
        # The closed L captured at admission never advances during a side turn.
        return None

    async def execute_turn_effect_async(self, continuation, effect):
        if isinstance(effect, LLMPreflightEffect):
            self._ensure_not_interrupted(continuation)
            await self._ensure_l1_turn_async(continuation, effect.assembly_context)
            prompt = self.build_turn_prompt(
                continuation, effect.assembly_context,
                max_output_tokens=effect.max_output_tokens,
                tools=self._resolve_llm_tools(continuation, effect.tools_override),
            )
            advice = await self.context.require_port("llm:llm").apreflight(LLMPreflightRequest(request=prompt))
            continuation.prompt_budget_snapshot = dict(advice.breakdown or {})
            if advice.status != LLMPreflightStatus.READY or self._is_hard_budget_overflow(advice):
                self.failed = True
                continuation.budget_failure_feedback_text = (
                    "The side question exceeds the available context budget. "
                    "Please shorten the question or continue in the main conversation."
                )
                advice = replace(advice, status=LLMPreflightStatus.READY)
            return EffectResult(status=RuntimeStatus.OK, payload=advice)
        if isinstance(effect, MemoryCompactEffect):
            self.failed = True
            return EffectResult(status=RuntimeStatus.ERROR, text=(
                "The side question exceeds the available context budget; "
                "the main conversation was not compacted. Please shorten the question."
            ))
        result = await super().execute_turn_effect_async(continuation, effect)
        if isinstance(effect, LLMRequestEffect) and (
            result.status == RuntimeStatus.ERROR
            or (isinstance(result.payload, LLMGenerationResult) and result.payload.finish_reason == LLMFinishReason.ERROR)
        ):
            self.failed = True
        return result

    async def _queue_llm_waiting_status(self, continuation, *, elapsed_seconds):
        output = self._agent_output_port()
        if output is not None and continuation.delivery_binding is not None:
            await self._call_output_port_async(
                output, "queue_status", continuation.delivery_binding, "llm_waiting",
                payload={"elapsed_seconds": elapsed_seconds,
                         "text": f"Side question is still processing · {int(elapsed_seconds)}s elapsed."},
            )


class BtwTurn:
    """One resident-session side slot; cleanup also covers pre-start cancellation."""

    def __init__(self, core: Any, action: ControlAction) -> None:
        self.core = core
        self.action = action
        self.turn_id = "btw:" + str(action.args.get("input_id") or uuid4().hex)
        envelope = core._route_to_channel_envelope(action.route)
        if envelope is None:
            raise ValueError("A side question requires a reply route")
        event = replace(
            envelope.event, event_id=self.turn_id, event_kind=EventKind.USER_MESSAGE,
            payload=LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR(BTW_INSTRUCTION + "\n\nUser question:\n" + str(action.args["text"])),),
                message_id=self.turn_id, semantic_kind="user_request",
            ),
        )
        source = core.context.require_port("memory:memory")
        # L can end in a closed prefix of the currently active main turn.
        # Its captured immutable messages become settled only in this branch.
        turns = tuple(replace(t, state=L1TurnState.SETTLED) for t in source.history_root.left_turns())
        self.memory = MemoryService(l1_store=InMemoryL1Store(L1TurnStore(turns)))
        self.memory.history_root.promote()
        self.context = copy(core.context)
        self.context.port_registry = PortRegistry(dict(core.context.port_registry))
        self.context.port_registry.pop(RESIDENT_HISTORY.name, None)
        self.context.port_registry["memory:memory"] = self.memory
        self.context.execution_runtime = core.context.execution_runtime.fork_transient()
        self.execution = self.context.execution_runtime
        self.snapshots = self.execution.result_snapshots
        self.llm_branch = None
        try:
            self.snapshots.bind_history(self.memory.history)
            main = core.state.active_turns.get(core.state.active_turn_id)
            settings = (deepcopy(main.turn_settings_snapshot) if main is not None
                        else core.turn_manager._build_turn_settings_snapshot())
            _, ids = core.turn_executor._left_view_seed(source, source.history_root)
            factory = core.context.port_registry.optional(LLM_SIDE_TURNS)
            if factory is None:
                raise ValueError("The current LLM provider does not support side questions")
            self.llm_branch = factory.create_side_turn(
                scope_id="pal:resident", branch_id=self.turn_id, message_ids=ids,
            )
            self.llm = self.llm_branch.runtime
            endpoint_id = (main.preferred_llm_endpoint_id if main is not None else None) or self.llm_branch.preferred_endpoint_id
            model_id = (main.preferred_llm_model_id if main is not None else None) or self.llm_branch.preferred_model_id
            self.context.port_registry["llm:llm"] = self.llm
            # ToolSurface returns fresh contract dictionaries backed by the
            # captured generation's immutable schemas. Retain those schemas;
            # deepcopy tries to mutate their frozen dict/list implementations.
            tools = core.turn_executor._build_llm_tool_contracts()
            self.runtime = AgentTurnRuntime.build(
                context=self.context, config=core.config,
                debug_log_prompt=lambda *_: None, debug_log_outcome=lambda *_: None,
                debug_log_reply=lambda *_: None, build_llm_tool_contracts=lambda: tools,
                handle_failure_async=None, render_failure_feedback_text=lambda value: str(value),
                should_enter_failure_flow_for_tool_result=lambda _: False,
                handle_llm_provider_errors=False, executor_type=BtwTurnExecutor,
            )
            self.continuation = TurnContinuation(
                turn_id=self.turn_id, opening_event=event,
                delivery_binding=envelope.opening_delivery_binding,
                correlation_id=action.route.correlation_id or self.turn_id,
                control_scope_key=action.route.control_scope_key,
                program=channel_turn_program(event, core_mode=core.state.mode,
                    max_output_tokens=core.turn_manager._resolve_max_output_tokens(
                        preferred_endpoint_id=endpoint_id)),
                turn_settings_snapshot=settings,
                preferred_llm_endpoint_id=endpoint_id,
                preferred_llm_model_id=model_id,
            )
        except BaseException:
            try:
                if self.llm_branch is not None:
                    self.llm_branch.release()
            finally:
                self.execution.unbind_result_history(self.memory)
            raise
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        core = self.core
        core.state.btw_turn_id = self.turn_id
        core.state.active_turns[self.turn_id] = self.continuation
        core.state.turn_scopes[self.turn_id] = self.continuation.control_scope_key
        self.event_payload = {**core._tracked_turn_event_payload(self.continuation), "turn_kind": "btw"}
        core.context.core_event_bus.emit(TURN_START, self.event_payload)
        self.task = asyncio.create_task(self.run())
        core.state.turn_tasks[self.turn_id] = self.task
        self.task.add_done_callback(self.finished)

    async def run(self) -> str:
        continuation = self.continuation
        result = None
        status = "failed"
        try:
            self.execution.begin_tool_result_turn(turn_id=self.turn_id, scope_key=self.turn_id, input_id=self.turn_id)
            while True:
                try:
                    effect = (next(continuation.program) if not continuation.started
                              else continuation.program.send(result))
                    continuation.started = True
                except StopIteration as stop:
                    if not isinstance(stop.value, TurnOutcome):
                        raise RuntimeError("Side turn ended without an outcome")
                    status = "failed" if self.runtime.executor.failed else "success"
                    break
                result = await self.runtime.executor.execute_turn_effect_async(continuation, effect)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.core.state.diagnostics.append({"kind": "btw.failed", "turn_id": self.turn_id,
                                                "error": f"{type(exc).__name__}: {exc}"})
            await self.core._reply_to_route_async(self.action.route, f"Side question failed: {exc}")
        finally:
            cleanup = asyncio.create_task(self.execution.finish_transient_turn(self.turn_id))
            cancelled = False
            try:
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.cancelled():
                            raise
                        # Reset/shutdown may arrive after cleanup has already
                        # started. Keep the slot and snapshot leases until the
                        # independently owned cleanup has actually exited.
                        cancelled = True
            except Exception as exc:
                status = "failed"
                self.core.state.diagnostics.append({"kind": "btw.cleanup", "turn_id": self.turn_id,
                                                    "error": f"{type(exc).__name__}: {exc}"})
            if cancelled:
                raise asyncio.CancelledError
        return status

    def finished(self, task: asyncio.Task[str]) -> None:
        status = "interrupted" if task.cancelled() else "failed"
        if not task.cancelled():
            try:
                status = task.result()
            except Exception as exc:
                self.core.state.diagnostics.append({"kind": "btw.cleanup", "error": str(exc)})
        try:
            # Each owner must get a chance to release even if another cleanup
            # fails. In particular, an output-file error cannot retain a slot.
            cleanup = (
                self.continuation.program.close,
                self.memory.history.clear,
                lambda: self.snapshots.finish_turn(self.turn_id),
                lambda: self.execution.unbind_result_history(self.memory),
                self.llm_branch.release,
                self.execution.logical_state.reset_state,
            )
            for release in cleanup:
                try:
                    release()
                except Exception as exc:
                    status = "failed"
                    self.core.state.diagnostics.append({"kind": "btw.cleanup", "turn_id": self.turn_id,
                                                        "error": f"{type(exc).__name__}: {exc}"})
        finally:
            core = self.core
            core.state.active_turns.pop(self.turn_id, None)
            core.state.turn_tasks.pop(self.turn_id, None)
            core.state.turn_scopes.pop(self.turn_id, None)
            if core.state.btw_turn_id == self.turn_id:
                core.state.btw_turn_id = None
                core._btw_turn = None
            core.context.core_event_bus.emit(TURN_END, {**self.event_payload, "status": status})
            core.notify_ready()


async def start_btw(core: Any, action: ControlAction) -> None:
    if core.state.resident_quiescing:
        await core._reply_to_route_async(action.route, "The runtime is stopping; please try the side question later.")
        return
    if core.state.btw_turn_id is not None:
        await core._reply_to_route_async(action.route, "A side question is already running; please wait for it to finish.")
        return
    try:
        turn = BtwTurn(core, action)
    except Exception as exc:
        await core._reply_to_route_async(action.route, f"Could not start side question: {exc}")
        return
    core._btw_turn = turn
    turn.start()


async def cancel_btw(core: Any) -> None:
    turn = core._btw_turn
    if turn is not None and turn.task is not None:
        turn.task.cancel()
        try:
            await turn.task
        except asyncio.CancelledError:
            pass
