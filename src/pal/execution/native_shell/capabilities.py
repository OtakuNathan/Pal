from __future__ import annotations

from typing import Literal
from pydantic import Field

from pal.execution.capabilities import ExecutionIntrospectionProvider
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, StrictToolModel, StructuredToolOutput, ToolGuidance, NextToolHint, ToolRejectedError
from pal.execution.tool_semantics import DIRECT_CONTROL, INDIRECT_CONTROL, INDIRECT_LOCAL_READ
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared import RuntimeStatus, capability_action

from .tools import RunInput, SessionInput


class NativeRunInput(RunInput):
    target: int = Field(default=0, ge=0, strict=True, description="Execution target; 0 is local. Discover configured remote target IDs with list_remote when needed; reuse a known target. Paths belong to this target.")
    sudo: bool = False


class RemoteStartInput(StrictToolModel):
    target: int = Field(gt=0, strict=True)
    action: str = Field(min_length=1)


class RemotePowerInput(StrictToolModel):
    target: int = Field(gt=0, strict=True)
    action: Literal["shutdown"] = "shutdown"


class DesktopRunInput(RunInput):
    sudo: bool = False


class ReconcileInput(StrictToolModel):
    operation_id: str = Field(min_length=1, max_length=128, description="Exact operation_id returned by a remote error affordance or shell_status.remote_operations.")


class ListRemoteInput(StrictToolModel):
    refresh: bool = False


class RecoverInput(StrictToolModel):
    call_id: str = Field(min_length=1, description="Exact call_id returned by the failed output recovery action or shell_status.retained_outputs.")


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
        InputModel=NativeRunInput, OutputModel=StructuredToolOutput, execution=DIRECT_CONTROL,
        async_handler_name="shell_async", metadata={"native_shell_action": "run"}, guidance=ToolGuidance(
            purpose="Run a shell command locally or on a configured remote target; return its result or a live session.",
            use_when=(
                "When the task already requires remote execution and the user has not specified a connection method, "
                "prefer run_shell(target=...) for configured targets. Use list_remote if the target mapping is unknown. "
                "The task determines the execution location; configured remotes do not change the local default. "
                "Honor an explicit request for SSH. Execute commands, builds or tests. Prefer rg for repository text search and rg --files for file"
                " enumeration; use alternatives only when rg is unavailable or unsuitable. Run tests and builds"
                " directly to preserve full output. wait_ms controls response waiting (default five minutes,"
                " one second for a PTY), not process lifetime; timeout_ms sets an optional hard deadline."
                " Use tty=true for interactive terminal input. A nonzero session_id means execution continues."
                " Completion is delivered to this channel separately; finish the turn when nothing else is needed."
                " Success requires status=exited and returncode=0. On Linux remote targets, sudo=true supports only"
                " apt/apt-get update or apt/apt-get install PACKAGE... without extra flags or shell operators;"
                " each requires approval. Inspect list_remote for configured management support."
            ),
            do_not_use_when=(
                "A dedicated file or Pal runtime/module/Bunshin introspection tool directly handles the task."
                " Never stop, restart or kill your own hosting service/process from an active turn; use subsystem"
                " lifecycle/hot-reload tools or hand a required full restart to the user or an external supervisor."
                " To change your own state, configuration, or endpoints, use a dedicated capability or the official"
                " `pal` CLI when it supports the change; never bypass it by hand-editing runtime storage, the database,"
                " or config files. For unsupported changes, follow pal.self.maintenance and the mutation policy."
                " Use file tools only for their own target; local file tools do not access remote paths. Use the selected"
                " target shell for remote files when no corresponding file capability exists. Do not pipe long-running"
                " tests/builds through head, tail or grep to shorten output; result budgeting handles it. Do not rerun a command"
                " that returned a live session or repeatedly poll it just to wait for completion."
            ),
            failure_next_steps=(
                "Inspect status, returncode, stdout and stderr before deciding whether repetition is safe."
                " Follow live-session affordances. For failed output delivery, use shell_recover_output;"
                " never repeat a command to retrieve output. A missing session does not prove it never ran."
            ),
            next_tool_hints=(
                NextToolHint(name="list_remote", use_when="The task needs remote execution and the configured target ID is unknown."),
                NextToolHint(name="shell_session", use_when="Inspect progress, send PTY input, resize or terminate a returned session."),
                NextToolHint(name="shell_status", use_when="A shell is blocked or retained output/completion needs diagnosis."),
            ),
        ),
    )
    def shell(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def shell_async(self, call):
        execution = call.meta["execution_runtime"]
        owner = execution.shell_owner
        budget = call.meta.get("budget")
        limit = execution._resolve_char_limit(budget) if budget is not None else None
        turn_id = str(call.meta.get("turn_id") or "")
        continuation = owner.core.state.active_turns.get(turn_id) if owner.core is not None else None
        delivery_context = {"origin_turn": turn_id, "budget": budget, "binding": getattr(continuation, "delivery_binding", None),
                            "committed": False, "cmd": call.args["cmd"], "tty": bool(call.args.get("tty"))}
        result = await owner.shell.run(**dict(call.args), turn_id=turn_id, delivery_context=delivery_context, retain_output=True, load_output=False,
                                       inline_limit=-1 if limit is None else min(limit, 2147483647))
        sid = result["session_id"]
        if sid:
            continuation = owner.core.state.active_turns.get(turn_id) if owner.core is not None else None
            owner.sessions[sid] = {
                "origin_turn": turn_id, "budget": budget,
                "binding": getattr(continuation, "delivery_binding", None),
                "committed": False, "cmd": call.args["cmd"], "tty": bool(call.args.get("tty")),
            }
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session", aliases=("shell_session",),
        InputModel=NativeSessionInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="session_async", metadata={"native_shell_action": "session"}, guidance=ToolGuidance(
            purpose="Inspect or control an existing shell session without rerunning its command.",
            use_when=(
                "Use the returned session_id. read returns a full snapshot; wait_ms waits for exit (default zero,"
                " maximum five minutes). Read for progress or interactive prompts, not in a short polling loop."
                " write queues exact PTY text (include a newline to submit); acceptance does not prove processing."
                " resize changes a live PTY. terminate requests cancellation; read terminal status to confirm exit."
                " release discards completed output. retry_notification retries a failed completion turn;"
                " inspect its previous effects before retrying."
            ),
            do_not_use_when=(
                "Do not invent IDs or use zero. Do not write or resize a non-PTY session, or release a running one."
                " Delivered terminal output is released automatically; use read_tool_result for its result_handle."
            ),
            failure_next_steps=(
                "For invalid_session, consult the previous result/result_handle or shell_status; reset or consumption"
                " may have retired it. Do not rerun the command automatically. For live-session precondition errors,"
                " read current status. After uncertain input delivery, inspect output before resending input."
            ),
        ),
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
        guidance=STATUS_GUIDANCE, async_handler_name="shell_status_async", metadata={"native_shell_action": "status"},
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
            "remote_operations": [{"operation_id": t.operation_id, "target": t.target, "runtime_epoch": t.epoch,
                                   "session_id": t.public_id} for t in owner._shell.operations.values()] if owner._shell else [],
        })

    async def shell_status_async(self, call):
        return self.shell_status(call)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="recover_output", aliases=("shell_recover_output",),
        InputModel=RecoverInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="recover_output_async", metadata={"native_shell_action": "recover_output"}, guidance=ToolGuidance(
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

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_list", aliases=("list_remote",),
        InputModel=ListRemoteInput, OutputModel=StructuredToolOutput, execution=INDIRECT_LOCAL_READ,
        async_handler_name="list_remote_async", metadata={"native_shell_action": "list_remote"}, guidance=ToolGuidance(
            purpose="List legal execution targets with configured facts and timestamped observed resources.",
            use_when="Choose a machine using OS, CPU architecture, shell and available compute; refresh probes without waking.",
            do_not_use_when="A returned session already fixes its target.",
            failure_next_steps="Offline entries remain valid targets; use only their configured explicit start actions.",
        ),
    )
    def list_remote(self, call):
        raise RuntimeError("Use asynchronous target discovery")

    async def list_remote_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        import os
        import platform
        items = [{"target": 0, "name": "local", "requires_wake": False, "registered": True,
                  "os": platform.system(), "arch": platform.machine(), "logical_cpus": os.cpu_count(),
                  "shell": {"executable": "/bin/bash", "invocation": ["-lc"]}}]
        if owner.remote_port is not None:
            items.extend(await owner.remote_port.list(call.args.get("refresh", False)))
        return self._result({"targets": items, "remote_attached": owner.remote_port is not None})

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="reconcile", aliases=("shell_reconcile",),
        InputModel=ReconcileInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="reconcile_async", metadata={"native_shell_action": "reconcile"}, guidance=ToolGuidance(
            purpose="Recover the outcome of an existing remote operation without submitting it again.",
            use_when="Submission or PTY input confirmation was lost and an operation_id was returned.",
            do_not_use_when="Output is already in a paged tool result.",
            failure_next_steps="UNKNOWN does not mean not executed; restore access to the original Runtime and query again.",
        ),
    )
    def reconcile(self, call):
        raise RuntimeError("Use asynchronous reconciliation")

    async def reconcile_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        result = await owner.shell.reconcile(call.args["operation_id"])
        if "session_id" not in result:
            return self._result(result)
        sid = result["session_id"]
        if sid and sid not in owner.sessions:
            ticket = owner.shell.tickets[sid]
            owner.sessions[sid] = dict(owner.shell.operation_context[ticket.operation_id])
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_start", aliases=("remote_start",),
        InputModel=RemoteStartInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="remote_start_async", metadata={"native_shell_action": "remote_start"}, guidance=ToolGuidance(
            purpose="Explicitly invoke one preconfigured target startup action.",
            use_when="list_remote reports a configured wake or user-service start action that is needed.",
            do_not_use_when="A target is already available; this is not command replay or worker restart.",
            failure_next_steps="Refresh target metadata to verify readiness; action completion alone does not prove readiness.",
        ),
    )
    def remote_start(self, call):
        raise RuntimeError("Use asynchronous remote management")

    async def remote_start_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result(await owner.shell._port().call('start', dict(call.args)))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_power", aliases=("remote_power",),
        InputModel=RemotePowerInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="remote_power_async", metadata={"native_shell_action": "remote_power"}, guidance=ToolGuidance(
            purpose="Request target shutdown after a single human approval and atomic worker busy check.",
            use_when="list_remote reports management.shutdown.supported=true and the target owner authorizes shutdown.",
            do_not_use_when="Only disconnecting the plugin or stopping one command is intended; never shut down this Pal host.",
            failure_next_steps="Accepted does not prove power-off. Reconcile UNKNOWN; busy rejects without scheduling later shutdown.",
        ),
    )
    def remote_power(self, call):
        raise RuntimeError("Use asynchronous remote management")

    async def remote_power_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result(await owner.shell.privileged(call.args['target'], 'shutdown', turn_id=str(call.meta.get('turn_id') or '')))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="shell_desktop", aliases=("run_shell_desktop",),
        InputModel=DesktopRunInput, OutputModel=StructuredToolOutput, execution=DIRECT_CONTROL,
        async_handler_name="desktop_async", metadata={"native_shell_action": "run"}, guidance=ToolGuidance(
            purpose="Run on the configured desktop target using the ordinary native shell contract.",
            use_when="The configured desktop is the intended execution location.",
            do_not_use_when="Another target is needed; use run_shell(target=...). This shortcut cannot override its target.",
            failure_next_steps="Inspect list_remote. No configured desktop means rejection, never local fallback.",
        ),
    )
    def desktop(self, call):
        raise RuntimeError("Use asynchronous native shell")

    async def desktop_async(self, call):
        from dataclasses import replace
        owner = call.meta['execution_runtime'].shell_owner
        targets = await owner.shell._port().list(False)
        desktops = [item['target'] for item in targets if item.get('shortcut') == 'desktop']
        if len(desktops) != 1:
            raise ToolRejectedError('Exactly one remote target must be configured with shortcut=desktop')
        return await self.shell_async(replace(call, args={**call.args, 'target': desktops[0]}))

    @staticmethod
    def _result(payload):
        text = render_structured_for_llm(payload)
        return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text)
