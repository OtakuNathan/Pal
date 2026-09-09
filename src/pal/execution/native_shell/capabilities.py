from __future__ import annotations

from typing import Literal
from pydantic import Field

from pal.execution.capabilities import ExecutionIntrospectionProvider
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, StrictToolModel, StructuredToolOutput, ToolGuidance, NextToolHint, ToolRejectedError
from pal.execution.tool_semantics import DIRECT_CONTROL, INDIRECT_CONTROL, INDIRECT_LOCAL_READ
from pal.shared import RuntimeStatus, capability_action

from .tools import RUN_GUIDANCE, SESSION_GUIDANCE, RunInput, SessionInput


class RecoverInput(StrictToolModel):
    call_id: str = Field(min_length=1)


class NativeSessionInput(SessionInput):
    action: Literal["read", "write", "resize", "terminate", "release", "retry_notification"] = "read"


STATUS_GUIDANCE = ToolGuidance(
    purpose="Show the shell backend, live/retained sessions and failed output deliveries.",
    use_when="A shell operation is blocked, a session ID is needed, or a completion notification failed.",
    do_not_use_when="The returned result already contains the session and next operation you need.",
    failure_next_steps="Inspect exec_show and core_observe if the execution backend itself is unavailable.",
    next_tool_hints=(NextToolHint(name="shell_session", use_when="Inspect or control a listed session."),
                     NextToolHint(name="shell_recover_output", use_when="A listed call has retained output awaiting delivery.")),
)


class NativeExecutionProvider(ExecutionIntrospectionProvider):
    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="shell", aliases=("run_shell",),
        InputModel=RunInput, OutputModel=StructuredToolOutput, execution=DIRECT_CONTROL,
        async_handler_name="shell_async", guidance=RUN_GUIDANCE.model_copy(update={
            "next_tool_hints": RUN_GUIDANCE.next_tool_hints + (NextToolHint(
                name="shell_status", use_when="A shell is blocked or retained output/completion needs diagnosis."),),
        }),
    )
    def shell(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def shell_async(self, call):
        execution = call.meta["execution_runtime"]
        owner = execution.shell_owner
        budget = call.meta.get("budget")
        limit = execution._resolve_char_limit(budget) if budget is not None else None
        turn_id = str(call.meta.get("turn_id") or "")
        result = await owner.shell.run(**dict(call.args), turn_id=turn_id, retain_output=True, load_output=False,
                                       inline_limit=-1 if limit is None else min(limit, 2147483647))
        sid = result["session_id"]
        if sid:
            continuation = owner.core.state.active_turns.get(turn_id) if owner.core is not None else None
            owner.sessions[sid] = {
                "origin_turn": turn_id, "budget": budget,
                "binding": getattr(continuation, "delivery_binding", None),
                "committed": False, "cmd": call.args["cmd"],
            }
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session", aliases=("shell_session",),
        InputModel=NativeSessionInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="session_async", guidance=SESSION_GUIDANCE.model_copy(update={
            "use_when": SESSION_GUIDANCE.use_when + " retry_notification explicitly retries a failed completion turn; inspect its previous effects first.",
        }),
    )
    def session(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        args = dict(call.args)
        if args.get("action") == "retry_notification":
            if owner.events is None:
                raise ToolRejectedError("No resident completion dispatcher is attached.")
            owner.events.retry(args["session_id"])
            return self._result({"session_id": args["session_id"], "status": "notification_retry_queued"})
        result = await owner.shell.session_snapshot(**args)
        if result["status"] == "released":
            owner.forget_session(result["session_id"])
            return self._result({"session_id": result["session_id"], "status": "released"})
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="status", aliases=("shell_status",),
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=INDIRECT_LOCAL_READ,
        guidance=STATUS_GUIDANCE, async_handler_name="shell_status_async",
    )
    def shell_status(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result({
            "backend": "native", "initialized": owner._shell is not None, "closed": owner.closed,
            "sessions": [{"session_id": sid, "cmd": item["cmd"], "origin_turn": item["origin_turn"],
                          "delivered": item["committed"]} for sid, item in owner.sessions.items()],
            "retained_outputs": [{"call_id": key, "session_id": item.result["session_id"], "status": item.result["status"]}
                                 for key, item in owner.pending.items()],
            "notification_failures": dict(owner.events.failures) if owner.events else {},
        })

    async def shell_status_async(self, call):
        return self.shell_status(call)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="recover_output", aliases=("shell_recover_output",),
        InputModel=RecoverInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="recover_output_async", guidance=ToolGuidance(
            purpose="Retry delivery of retained shell output without executing the command again.",
            use_when="A shell response failed during file reading, validation or pagination and returned this recovery action.",
            do_not_use_when="Output was already delivered; use its result_handle or live session instead.",
            failure_next_steps="Inspect shell_status for retained call IDs. Never rerun a command just to recover its output.",
        ),
    )
    def recover_output(self, call):
        raise RuntimeError("native output recovery requires asynchronous execution")

    async def recover_output_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        call_id = call.args["call_id"]
        pending = owner.pending.get(call_id)
        if pending is None:
            raise ToolRejectedError("No retained output for that call ID. Inspect shell_status; do not replay the command.")
        return await owner.stage(call, pending.result, recovery_of=call_id, raw=pending.raw)

    @staticmethod
    def _result(payload):
        import json
        text = json.dumps(payload, ensure_ascii=False)
        return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text)
