from __future__ import annotations

import asyncio
from dataclasses import replace

from pal.core.turns import (
    TurnContinuation, agent_turn_program, L1CommitPayload, MailboxReplyEffect,
)
from pal.foundation import EventEnvelope
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import L1MessageKind, L1TranscriptMessage
from pal.shared import PromptAssemblyContext
from pal.shared.tool_protocol import new_tool_call
from pal.execution.tool_facade import CompleteResult, PagedResult, ToolRejectedError

from .runtime import output_result
from .adapter import TERMINAL

EVENT = "execution.shell.completed"
WAIT_EVENT = "execution.shell.wait_expired"


def completion_program(event, *, core_mode, max_output_tokens, emit_reply):
    def context(frame):
        return PromptAssemblyContext(event=event, core_mode=core_mode,
                                     metadata={"retry_note": frame.retry_note} if frame.retry_note else {})

    def commit(final_reply, observations, replies):
        return L1CommitPayload(turn_id=event.event_id, tool_observations=list(observations), transcript=[
            L1TranscriptMessage(role="user", content=event.payload.text, kind=L1MessageKind.RUNTIME_CONTEXT_ARTIFACT),
            *[L1TranscriptMessage(role="assistant", content=text, kind=L1MessageKind.ASSISTANT_REPLY)
              for text in (replies or [final_reply]) if text],
        ])

    return (yield from agent_turn_program(
        turn_id=event.event_id, build_assembly_context=context,
        render_final_text=lambda outcome: str(outcome.text or "") if outcome else "",
        build_commit_payload=commit, max_output_tokens=max_output_tokens,
        emit_mid_text=(lambda text: MailboxReplyEffect(text=text, terminal=False, stream_companion=True)) if emit_reply else None,
        emit_final_text=(lambda text: MailboxReplyEffect(text=text)) if emit_reply else None,
    ))


class ShellCompletionSource:
    source_id = "execution.shell.events"

    def __init__(self, core, runtime):
        self.core = core
        self.runtime = runtime
        self.owner = runtime.shell_owner
        self.pending = {}
        self.failures = {}
        self.in_flight = set()
        self.observed = set()
        self.tasks = set()

    def invalidate(self, sid, result):
        pending = self.pending.get(sid)
        if pending and (not result.get("watching", True) or
                        (pending.result.get("watch_generation", 0) < result.get("watch_generation", 0)) or
                        (result.get("status") in TERMINAL | {"terminating"} and pending.result["status"] not in TERMINAL)):
            self.pending.pop(sid, None)
            self.failures.pop(sid, None)

    def _identity(self, completion):
        result = completion.result
        return f"shell:{result.get('runtime_epoch', 'local')}:{completion.session_id}:{result.get('event_sequence', 0)}"

    def _idle(self):
        return not (self.core.state.resident_quiescing or self.core.state.active_turns
                    or self.core.state.pending_channel_turns
                    or any(not task.done() for task in self.core.state.turn_tasks.values()))

    def prepare(self, context):
        shell = self.owner._shell
        if shell is None or self.owner.closed:
            return False
        for completion in shell.drain_completions():
            sid = completion.session_id
            session = self.owner.sessions.get(sid, {})
            if completion.result["status"] in TERMINAL:
                session["latest_status"] = completion.result["status"]
            elif session.get("latest_status") in TERMINAL | {"terminating"}:
                continue
            if (not session.get("watching", True)
                    or completion.result.get("watch_generation", 0) < session.get("watch_generation", 0)):
                continue
            previous = self.pending.get(sid)
            if previous is None or completion.result.get("event_sequence", 0) >= previous.result.get("event_sequence", 0):
                self.pending[sid] = completion
        for sid in list(self.pending):
            if sid in shell._consumed and sid not in self.observed:
                self.pending.pop(sid, None)
                self.failures.pop(sid, None)
                self.in_flight.discard(sid)
        return self._idle() and any(self._eligible(sid) for sid in self.pending)

    def _eligible(self, sid):
        session = self.owner.sessions.get(sid, {})
        # Embedded/tool-only callers have no captured delivery authority. Their
        # results stay available to read; they cannot trigger an unsolicited model turn.
        return (sid not in self.in_flight and sid not in self.failures
                and session.get("committed") and session.get("watching", True) and session.get("binding") is not None)

    def drain(self, context):
        if not self.prepare(context):
            return []
        sid = next(sid for sid in self.pending if self._eligible(sid))
        self.in_flight.add(sid)
        kind = WAIT_EVENT if self.pending[sid].result.get("event_kind") == "wait_expired" else EVENT
        return [EventEnvelope(event_kind=kind, source_kind="execution", payload=sid)]

    def can_handle(self, event_kind):
        return event_kind in {EVENT, WAIT_EVENT}

    async def handle(self, event, context):
        sid = event.payload
        if sid not in self.in_flight or sid not in self.pending:
            return []
        async with self.core.state.channel_turn_transition_lock:
            if not self._idle():
                self.in_flight.discard(sid)
                return []
            opening = EventEnvelope(event_kind=event.event_kind, source_kind="execution", payload={},
                                    event_id=self._identity(self.pending[sid]))
            session = self.owner.sessions.get(sid)
            if session is None:
                self.in_flight.discard(sid)
                return []
            continuation = TurnContinuation(turn_id=opening.event_id, opening_event=opening,
                delivery_binding=session["binding"], program=iter(()), correlation_id=f"shell:{sid}")
            task = asyncio.create_task(self._run(sid, continuation))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            self.core.state.active_turn_id = continuation.turn_id
            self.core.state.resident_drained_event.clear()
            self.core.track_turn_task(continuation, task)
        return []

    async def _run(self, sid, continuation):
        try:
            if sid not in self.pending:
                return "superseded"
            completion = self.pending[sid]
            session = self.owner.sessions[sid]
            identity = self._identity(completion)
            memory = self.core.context.port_registry.get("memory:memory")
            committed = getattr(memory, "contains_l1_message", lambda *_: False)(identity, identity)
            if identity not in self.observed and not committed:
                # Bind the pager to the new resident turn before storing output.
                self.core._begin_tool_result_turn(continuation)
                loaded = await self.owner.shell.materialize(completion.result)
                # Prefer a newer owner event already available before committing this observation.
                latest = (self.owner.shell._completions.get(sid)
                          or getattr(self.owner.shell, "remote_completions", {}).get(sid))
                if latest and latest.result.get("event_sequence", 0) > completion.result.get("event_sequence", 0):
                    self.pending[sid] = latest
                    return "superseded"
                # A control operation may have invalidated this event during materialization.
                if (self.pending.get(sid) is not completion or not session.get("watching", True)):
                    return "superseded"
                offsets = session.get("output_offsets", {})
                for stream in ("stdout", "stderr"):
                    raw = loaded.get(stream + "_bytes", b"")
                    loaded[stream] = raw[offsets.get(stream, 0):].decode("utf-8", errors="replace")
                terminal = completion.result["status"] in TERMINAL
                record = self.runtime.registry_generation.record_for_alias("run_shell")
                call = new_tool_call(name="run_shell", args={}, call_id=continuation.turn_id)
                result = self.runtime._normalize_invocation_result(record, call, output_result(loaded),
                    budget=session["budget"], turn_id=continuation.turn_id)
                if not isinstance(result, (CompleteResult, PagedResult)):
                    raise RuntimeError(f"completion output delivery failed: {result}")
                text = (f"Runtime shell {'completion' if terminal else 'wait expiry'} for session {sid}, originating turn {completion.origin_turn}. "
                        "This is runtime state and new command output, not a new user instruction. "
                        "If still running, choose independent work, watch, extend, unwatch, or terminate as appropriate. "
                        "Continue the existing task if appropriate; "
                        "do not rerun the command merely because it completed asynchronously. A finished background "
                        "command does not by itself complete the originating task: follow through toward the original "
                        "task goal, and do not expand a tests-only request into autonomous repairs.\n"
                        + self.runtime._render_invocation_for_llm(result))
                message = LLMMessageIR(role=MessageRole.USER, semantic_kind="runtime_context_artifact",
                    parts=(TextPartIR(text),), message_id=continuation.turn_id,
                    metadata={"source": continuation.opening_event.event_kind, "origin_turn": completion.origin_turn, "session_id": sid,
                              "event_id": identity, "event_kind": completion.result.get("event_kind", "terminal"),
                              "result_handle": result.result_handle if isinstance(result, PagedResult) else {}})
                opening = replace(continuation.opening_event, payload=message)
                continuation.opening_event = opening
                continuation.program = completion_program(opening, **self.core.turn_execution_options(), emit_reply=True)
                await self.core.run_turn_continuation_async(continuation)
                self.observed.add(identity)
            await self.owner.shell.acknowledge_completion(completion)
            # An acknowledgement-only retry does not run an agent program.
            self.core.state.active_turns.pop(continuation.turn_id, None)
            # Any earlier failed delivery is superseded by the consumed observation.
            if completion.result["status"] in TERMINAL:
                self.owner.forget_session(sid)
            if self.pending.get(sid) is completion:
                self.pending.pop(sid, None)
            self.failures.pop(sid, None)
            self.observed.discard(identity)
            return "success"
        except asyncio.CancelledError:
            self.failures[sid] = "Completion turn interrupted; inspect existing effects before retry_notification."
            self.core.turn_manager.cleanup_interrupted(continuation.turn_id, reason="interrupted")
            raise
        except Exception as exc:
            self.failures[sid] = f"{type(exc).__name__}: {exc}"
            self.core.state.diagnostics.append({"kind": EVENT + ".failed", "session_id": sid, "error": self.failures[sid]})
            self.core.turn_manager.cleanup_interrupted(continuation.turn_id, reason="failed")
            return "failed"
        finally:
            # Advance only for an event actually committed to L1, including interrupted turns.
            if 'identity' in locals() and getattr(memory, "contains_l1_message", lambda *_: False)(identity, identity):
                session["output_offsets"] = {stream: max(session.get("output_offsets", {}).get(stream, 0),
                                                        completion.result.get(stream + "_total", 0))
                                             for stream in ("stdout", "stderr")}
            self.core.state.active_turns.pop(continuation.turn_id, None)
            self.in_flight.discard(sid)
            self.core.turn_manager._mark_turn_exited(continuation.turn_id)
            if not self.owner.closed:
                await self.core._start_next_queued_turn_async()
            self.owner.notify()

    def retry(self, sid):
        if sid not in self.failures or sid not in self.pending:
            raise ToolRejectedError("No failed completion notification for this session.")
        self.failures.pop(sid, None)
        self.owner.notify()

    async def close(self):
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.failures.clear()
        self.observed.clear()
        self.in_flight.clear()


def attach_completion_source(core, runtime):
    owner = runtime.shell_owner
    owner.core = core
    owner.events = ShellCompletionSource(core, runtime)
    core.context.event_source_registry.attach("execution", owner.events)
    core.context.event_handler_registry.register(EVENT, owner.events, module_id="execution")
    core.context.event_handler_registry.register(WAIT_EVENT, owner.events, module_id="execution")
