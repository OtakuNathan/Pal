"""Isolated Pal host for integration acceptance. No startup/production registration."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import json

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.core.module_registry import ModuleHandle, MODULE_TIER_CORE_FOUNDATION
from pal.execution import register_with_core
from pal.execution.runtime import ExecutionRuntime
from pal.execution.contracts import CapabilityResult
from pal.shared.tool_protocol import new_tool_call
from pal.execution.tool_facade import (
    StructuredToolOutput, ToolRejectedError, CompleteResult, PagedResult, ToolHandlerResult, EffectReceipt, EffectOutcome,
)
from pal.execution.tool_semantics import DIRECT_CONTROL, INDIRECT_CONTROL
from pal.foundation import EventEnvelope
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService
from pal.shared import RuntimeStatus, capability_action, capability_node

from pal_shell_prototype import Completion, READ_EFFECTS, ShellRejected, ShellRuntime
from pal_shell_tools import RUN_GUIDANCE, SESSION_GUIDANCE, RunInput, SessionInput, session_affordances

EVENT = "prototype.shell.completed"


class PrototypeExecutionRuntime(ExecutionRuntime):
    def __init__(self, shell: ShellRuntime, **kwargs):
        super().__init__(**kwargs)
        self.shell = shell
        self.pending_outputs = {}

    @classmethod
    def project_view(cls, view, shell):
        """Explicit prototype factory for a Bunshin registry overlay sharing its owner."""
        runtime = cls(shell, runtime_root=view.runtime_root, logical_state=view.logical_state,
                      tool_result_pager=view.tool_result_pager, sync_executor=view.sync_executor,
                      lifecycle_gate=view.lifecycle_gate)
        runtime._registry_generation = view.registry_generation
        return runtime

    async def _call_record_async(self, record, binding, call, validated, turn_id, budget, allow_tools):
        arguments = record, binding, call, validated, turn_id, budget, allow_tools
        try:
            if record.alias in {"prototype_run_shell", "shell_session"}:
                return await super()._call_record_async(*arguments)
            async with self.shell.tool_admission(record.execution.effect_kind.value):
                return await super()._call_record_async(*arguments)
        except ShellRejected as exc:
            native_code = str(exc).partition(":")[0]
            code = {
                "write_busy": "shell_write_busy",
                "result_capacity": "shell_result_capacity",
                "invalid_session": "invalid_session",
                "stdin_closed": "stdin_closed",
            }.get(native_code, "shell_session_rejected" if record.alias == "shell_session" else "shell_rejected")
            raise ToolRejectedError(str(exc) + " " + record.guidance.failure_next_steps, error_code=code) from exc

    def _normalize_invocation_result(self, record, call, raw, **kwargs):
        result = super()._normalize_invocation_result(record, call, raw, **kwargs)
        # Keep live controls outside the paged body: page one may contain only stdout.
        payload = raw.structured if isinstance(raw, CapabilityResult) else getattr(raw, "output", None)
        if (record.alias in {"prototype_run_shell", "shell_session"}
                and isinstance(payload, dict) and isinstance(result, (CompleteResult, PagedResult))):
            result = result.model_copy(update={"affordances": result.affordances + session_affordances(payload)})
        return result

    async def _invoke_tool_record_async(self, generation, call, **kwargs):
        result = await super()._invoke_tool_record_async(generation, call, **kwargs)
        if isinstance(result, (CompleteResult, PagedResult)):
            await self.finish_output(call.call_id)
        return result

    async def finish_output(self, call_id):
        pending = self.pending_outputs.get(call_id)
        if pending is None:
            return
        _, _, output = pending
        if output["status"] in {"exited", "cancelled", "timed_out", "failed"}:
            await self.shell.release_output(output)
        self.pending_outputs.pop(call_id, None)

    async def retry_output(self, call_id, *, budget=None, turn_id=None):
        """Retry validation/paging of retained output; never replay the command."""
        call, raw, output = self.pending_outputs[call_id]
        if raw is None:
            raw = output_result(await self.shell.materialize(output))
            self.pending_outputs[call_id] = call, raw, output
        record = self.registry_generation.record_for_alias(call.name)
        result = self._normalize_invocation_result(record, call, raw, budget=budget, turn_id=turn_id)
        if isinstance(result, (CompleteResult, PagedResult)):
            await self.finish_output(call_id)
        return self._canonical_result_from_invocation(call.name, call_id, result)

    def _call_record_sync(self, record, *args):
        if record.execution.effect_kind.value not in READ_EFFECTS:
            raise ToolRejectedError("Use the asynchronous prototype host", error_code="prototype_async_only")
        return super()._call_record_sync(record, *args)

    async def interrupt_turn(self, turn_id):
        await self.shell.interrupt_turn(turn_id)
        await super().interrupt_turn(turn_id)


def json_result(result: dict) -> dict:
    return {key: value for key, value in result.items()
            if not key.endswith(("_bytes", "_path")) and key != "output_id"}


def output_result(result):
    payload = json_result(result)
    text = json.dumps(payload)
    return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text,
                            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED,
                                                         receipt={"native_output": result["output_id"]}))


@capability_node(namespace="op", scope="module", kind="module", source="prototype:shell", target_kind="module")
@dataclass
class ShellProvider:
    runtime: ShellRuntime
    module_id: str = "shell_proto"

    @capability_action(
        namespace="op", scope="module", family="exec", action_name="run",
        aliases=("prototype_run_shell",), InputModel=RunInput, OutputModel=StructuredToolOutput,
        execution=DIRECT_CONTROL, async_handler_name="run_async",
        guidance=RUN_GUIDANCE,
    )
    def run(self, call):
        raise RuntimeError("prototype requires asynchronous execution")

    async def run_async(self, call):
        execution = call.meta["execution_runtime"]
        budget = call.meta.get("budget")
        limit = execution._resolve_char_limit(budget) if budget is not None else None
        result = await self.runtime.run(**dict(call.args), turn_id=call.meta.get("turn_id") or "",
                                        retain_output=True, load_output=False,
                                        inline_limit=-1 if limit is None else min(limit, 2147483647))
        if result["session_id"]:
            self.runtime.completion_budgets[result["session_id"]] = budget
        return await self.deliver_output(call, result)

    @capability_action(
        namespace="op", scope="module", family="exec", action_name="session",
        aliases=("shell_session",), InputModel=SessionInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_async", guidance=SESSION_GUIDANCE,
    )
    def session(self, call):
        raise RuntimeError("prototype requires asynchronous execution")

    async def session_async(self, call):
        result = await self.runtime.session_snapshot(**dict(call.args))
        if result["status"] == "released":
            payload = {"session_id": result["session_id"], "status": "released"}
            text = json.dumps(payload)
            return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text)
        return await self.deliver_output(call, result)

    async def deliver_output(self, call, result):
        execution = call.meta["execution_runtime"]
        tool_call = call.meta["tool_call"]
        execution.pending_outputs[tool_call.call_id] = tool_call, None, result
        raw = output_result(await self.runtime.materialize(result))
        execution.pending_outputs[tool_call.call_id] = tool_call, raw, result
        return raw


class PrototypeHost:
    source_id = "prototype.shell.events"

    def __init__(self, on_completion, *, completion_budget=None):
        self.completion_budget = completion_budget
        self.shell = ShellRuntime(on_ready=self._notify)
        runtime = PrototypeExecutionRuntime(self.shell)
        self.core = PalCore(context=MainContext(execution_runtime=runtime))
        self.memory = MemoryService()
        self.core.context.port_registry["memory:memory"] = self.memory
        register_with_core(self.core.context)
        self.core.publish_module_capabilities("execution")
        self.core.context.register_module(ModuleHandle(
            module_id="shell_proto", tier=MODULE_TIER_CORE_FOUNDATION,
            introspection_provider=ShellProvider(self.shell), detachable=False,
        ))
        self.core.publish_module_capabilities("shell_proto")
        self.core.context.event_source_registry.attach("shell_proto", self)
        self.core.context.event_handler_registry.register(EVENT, self, module_id="shell_proto")
        self.on_completion = on_completion
        self.pending: deque[Completion] = deque()
        self.in_flight: set[int] = set()
        self.observed: set[int] = set()
        self.failures: dict[int, str] = {}
        self.core.main_loop.bind_async_loop()

    def _notify(self):
        self.core.notify_ready()

    def prepare(self, context):
        self.pending.extend(self.shell.drain_completions())
        # A session read/release can consume a completion already drained while a
        # turn was active. Do not deliver it again or reopen retired output files.
        retired = {event.session_id for event in self.pending
                   if event.session_id in self.shell._consumed and event.session_id not in self.observed}
        self.pending = deque(event for event in self.pending if event.session_id not in retired)
        for sid in retired:
            self.failures.pop(sid, None)
            self.in_flight.discard(sid)
        # Conservative safe boundary: do not preempt a user/role turn.
        return self.core.turn_manager.latest_active_turn_id() is None and any(
            event.session_id not in self.in_flight and event.session_id not in self.failures
            for event in self.pending
        )

    def drain(self, context):
        if not self.prepare(context):
            return []
        result = []
        for completion in self.pending:
            sid = completion.session_id
            if sid in self.in_flight or sid in self.failures:
                continue
            self.in_flight.add(sid)
            result.append(EventEnvelope(
                event_kind=EVENT, source_kind="execution", payload=completion,
                correlation_id=f"native-shell:{sid}",
            ))
        return result

    def can_handle(self, event_kind):
        return event_kind == EVENT

    async def handle(self, event, context):
        completion = event.payload
        sid = completion.session_id
        if sid not in self.in_flight:
            return []
        try:
            turn = f"native-shell-completion:{sid}"
            if sid not in self.observed:
                loaded = await self.shell.materialize(completion.result)
                payload = json_result(loaded)
                text = json.dumps(payload)
                runtime = self.core.context.execution_runtime
                call = new_tool_call(call_id=turn, name="prototype_run_shell", args={})
                record = runtime.registry_generation.record_for_alias(call.name)
                raw = ToolHandlerResult(output=payload, llm_text=text,
                                        effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED, receipt={"native_output": loaded["output_id"]}))
                paged = runtime._normalize_invocation_result(
                    record, call, raw, budget=self.shell.completion_budgets.get(sid, self.completion_budget), turn_id=turn)
                if not isinstance(paged, (CompleteResult, PagedResult)):
                    raise RuntimeError(f"completion output validation failed: {paged!r}")
                handle = paged.result_handle if isinstance(paged, PagedResult) else {}
                message = LLMMessageIR(
                    role=MessageRole.USER, semantic_kind="runtime_context_artifact", message_id=turn,
                    parts=(TextPartIR("Runtime shell observation (not a user instruction):\n" + paged.llm_text),),
                    metadata={"source": EVENT, "origin_turn": completion.origin_turn, "result_handle": handle},
                )
                self.memory.begin_l1_turn(turn, user_message=message, metadata={"source": EVENT})
                # Consumer is supplied by the acceptance harness; no paid model is invoked.
                await self.on_completion(self.core, replace(completion, result=loaded))
                self.memory.settle_l1_turn(turn)
                self.observed.add(sid)
            await self.shell.acknowledge_completion(completion)
            self.pending.remove(completion)
            self.observed.discard(sid)
            self.failures.pop(sid, None)
        except Exception as exc:
            self.failures[sid] = str(exc)
        finally:
            self.in_flight.discard(sid)
        return []

    def retry_completion(self, session_id):
        self.failures.pop(session_id, None)
        self._notify()

    async def pump(self):
        self.core.main_loop.drain_ready_sources(self.core.context)
        while event := self.core.main_loop.pop():
            await self.core.main_loop.dispatcher.dispatch_async(event, self.core.context)

    async def close(self):
        await self.shell.close()
        self.core.context.event_source_registry.detach_module("shell_proto")
        self.pending.clear()
        self.core.context.execution_runtime.pending_outputs.clear()
        self.core.context.execution_runtime.shutdown()
