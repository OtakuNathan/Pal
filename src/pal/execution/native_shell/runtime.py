from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json

from pal.execution.contracts import CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    CompleteResult, PagedResult, EffectOutcome, EffectReceipt, ToolRejectedError,
    ToolAffordance,
)
from pal.shared import RuntimeStatus

from .adapter import ShellRuntime, ShellRejected, TERMINAL, READ_EFFECTS
from .tools import session_affordances


NATIVE_TOOLS = frozenset({"run_shell", "shell_session", "shell_status", "shell_recover_output"})


def output_result(result):
    payload = {key: value for key, value in result.items()
               if not key.endswith(("_bytes", "_path")) and key not in {"output_id", "request_id"}}
    if result.get("status") not in TERMINAL:
        payload["returncode"] = None
    text = json.dumps(payload, ensure_ascii=False)
    return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text,
                            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED))


@dataclass
class PendingOutput:
    result: dict
    turn_id: str
    raw: CapabilityResult | None = None
    prepared: bool = False
    recovery_of: str = ""


class NativeShellOwner:
    """One process/write owner, shared by immutable registry projections."""

    def __init__(self):
        self._shell = None
        self.core = None
        self.events = None
        self.pending = {}
        self.sessions = {}
        self.closed = False

    @property
    def shell(self):
        if self.closed:
            raise ShellRejected("shell runtime is closed")
        if self._shell is None:
            self._shell = ShellRuntime(on_ready=self.notify)
        return self._shell

    def notify(self):
        if self.core is not None:
            self.core.notify_ready()

    async def stage(self, call, result, *, recovery_of="", raw=None):
        tool_call = call.meta["tool_call"]
        pending = PendingOutput(result, str(call.meta.get("turn_id") or ""), recovery_of=recovery_of)
        self.pending[tool_call.call_id] = pending
        pending.raw = raw if raw is not None else output_result(await self.shell.materialize(result))
        return pending.raw

    async def commit(self, call_id):
        pending = self.pending.get(call_id)
        if pending is None:
            return
        sid = pending.result["session_id"]
        session = self.sessions.get(sid)
        if session is not None:
            session["committed"] = True
        if not pending.prepared:
            self.notify()
            return  # Retain failed materialization/paging for explicit recovery.
        if pending.result["status"] in TERMINAL:
            await self.shell.release_output(pending.result)
            self.sessions.pop(sid, None)
            # A terminal full snapshot supersedes every failed handoff of this
            # output, including chains of failed recovery calls.
            for key, alias in list(self.pending.items()):
                if alias.result["output_id"] == pending.result["output_id"]:
                    self.pending.pop(key, None)
        else:
            # For a live partial snapshot, retire just its recovery ancestry.
            while (retired := self.pending.pop(call_id, None)) is not None:
                call_id = retired.recovery_of
        self.notify()

    async def interrupt(self, turn_id):
        if self._shell is None:
            return
        await self.shell.interrupt_turn(turn_id)
        # The adapter's return alone does not establish delivery to the model.
        for sid, session in list(self.sessions.items()):
            if session["origin_turn"] == turn_id and not session["committed"]:
                with suppress(ShellRejected):
                    await self.shell.terminate(sid)
                    await self.shell._discard_cancelled_session(sid)
                self.sessions.pop(sid, None)
        for call_id, pending in list(self.pending.items()):
            if pending.turn_id == turn_id and pending.result["session_id"] not in self.sessions:
                with suppress(ShellRejected):
                    await self.shell.release_output(pending.result)
                self.pending.pop(call_id, None)

    def forget_session(self, session_id):
        """Drop host references after native output was explicitly retired."""
        if not session_id:
            return  # Zero identifies many independent one-shot results.
        self.sessions.pop(session_id, None)
        for call_id, pending in list(self.pending.items()):
            if pending.result["session_id"] == session_id:
                self.pending.pop(call_id, None)

    async def close(self):
        self.closed = True
        if self.events is not None:
            await self.events.close()
        if self._shell is not None:
            await self._shell.close()
        self._shell = None
        self.pending.clear()
        self.sessions.clear()

    async def reset(self):
        await self.close()
        self.closed = False


class NativeExecutionRuntime(ExecutionRuntime):
    def __init__(self, *, owner=None, **kwargs):
        super().__init__(**kwargs)
        self.shell_owner = owner or NativeShellOwner()
        self._owns_shell = owner is None

    @classmethod
    def project_view(cls, view, owner):
        runtime = cls(owner=owner, runtime_root=view.runtime_root, logical_state=view.logical_state,
                      tool_result_pager=view.tool_result_pager, sync_executor=view.sync_executor,
                      lifecycle_gate=view.lifecycle_gate)
        runtime._registry_generation = view.registry_generation
        return runtime

    async def _call_record_async(self, record, binding, call, validated, turn_id, budget, allow_tools):
        arguments = record, binding, call, validated, turn_id, budget, allow_tools
        try:
            if record.alias in NATIVE_TOOLS:
                return await super()._call_record_async(*arguments)
            async with self.shell_owner.shell.tool_admission(record.execution.effect_kind.value):
                return await super()._call_record_async(*arguments)
        except ShellRejected as exc:
            prefix = str(exc).partition(":")[0]
            code = {"write_busy": "shell_write_busy", "invalid_session": "invalid_session",
                    "result_capacity": "shell_result_capacity", "stdin_closed": "stdin_closed"}.get(prefix, "shell_rejected")
            raise ToolRejectedError(str(exc) + " " + record.guidance.failure_next_steps, error_code=code,
                                    affordances=[ToolAffordance(tool="call_tool", arguments={"name": "shell_status", "args": {}},
                                                               reason="Inspect active sessions and retained outputs before retrying.")]) from exc

    def _call_record_sync(self, record, *args):
        if record.execution.effect_kind.value not in READ_EFFECTS:
            raise ToolRejectedError("Native shell mode requires asynchronous execution for writes/control.",
                                    error_code="native_async_required")
        return super()._call_record_sync(record, *args)

    def _normalize_invocation_result(self, record, call, raw, **kwargs):
        result = super()._normalize_invocation_result(record, call, raw, **kwargs)
        if record.alias not in NATIVE_TOOLS:
            return result
        pending = self.shell_owner.pending.get(call.call_id)
        if isinstance(result, (CompleteResult, PagedResult)):
            if pending:
                pending.prepared = True
            payload = raw.structured if isinstance(raw, CapabilityResult) else getattr(raw, "output", None)
            if isinstance(payload, dict):
                result = result.model_copy(update={"affordances": result.affordances + session_affordances(payload)})
        return result

    async def _invoke_tool_record_async(self, generation, call, **kwargs):
        result = await super()._invoke_tool_record_async(generation, call, **kwargs)
        if call.name not in NATIVE_TOOLS:
            return result  # call_tool's resolved inner invocation owns the handoff.
        pending = self.shell_owner.pending.get(call.call_id)
        if pending is None:
            return result
        if not isinstance(result, (CompleteResult, PagedResult)):
            result = result.model_copy(update={"affordances": result.affordances + [ToolAffordance(
                tool="call_tool", arguments={"name": "shell_recover_output", "args": {"call_id": call.call_id}},
                reason="Retry retained output delivery only; the command must not be replayed.")],
                "effect": EffectOutcome.APPLIED})
        core = self.shell_owner.core
        if core is None or pending.turn_id not in core.state.active_turns:
            # Embedded callers own delivery at the returned result boundary.
            await self.shell_owner.commit(call.call_id)
        return result

    async def acknowledge_tool_result_async(self, call_id, turn_id):
        pending = self.shell_owner.pending.get(call_id)
        if pending is not None and pending.turn_id == turn_id:
            await self.shell_owner.commit(call_id)

    async def interrupt_turn(self, turn_id):
        await self.shell_owner.interrupt(turn_id)
        await super().interrupt_turn(turn_id)

    async def shutdown_async(self):
        if self._owns_shell:
            await self.shell_owner.close()
            super().shutdown()

    async def prepare_shutdown_async(self):
        # Native process handles cannot be checkpointed across a process exit.
        # Quiesce/reap them before the ordinary L1/execution snapshot is saved.
        await self.shell_owner.close()

    def shutdown(self):
        if self._owns_shell and self.shell_owner._shell is not None:
            raise RuntimeError("await shutdown_async() before closing a native execution runtime")
        if self._owns_shell:
            super().shutdown()
