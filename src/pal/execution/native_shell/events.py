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

EVENT = "execution.shell.completed"


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

    def _idle(self):
        return not (self.core.state.resident_quiescing or self.core.state.active_turns
                    or self.core.state.pending_channel_turns
                    or any(not task.done() for task in self.core.state.turn_tasks.values()))

    def prepare(self, context):
        shell = self.owner._shell
        if shell is None or self.owner.closed:
            return False
        for completion in shell.drain_completions():
            self.pending[completion.session_id] = completion
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
                and session.get("committed") and session.get("binding") is not None)

    def drain(self, context):
        if not self.prepare(context):
            return []
        sid = next(sid for sid in self.pending if self._eligible(sid))
        self.in_flight.add(sid)
        return [EventEnvelope(event_kind=EVENT, source_kind="execution", payload=sid)]

    def can_handle(self, event_kind):
        return event_kind == EVENT

    async def handle(self, event, context):
        sid = event.payload
        if sid not in self.in_flight or sid not in self.pending:
            return []
        async with self.core.state.channel_turn_transition_lock:
            if not self._idle():
                self.in_flight.discard(sid)
                return []
            opening = EventEnvelope(event_kind=EVENT, source_kind="execution", payload={})
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
            completion = self.pending[sid]
            session = self.owner.sessions[sid]
            if sid not in self.observed:
                # Bind the pager to the new resident turn before storing output.
                self.core._begin_tool_result_turn(continuation)
                loaded = await self.owner.shell.materialize(completion.result)
                record = self.runtime.registry_generation.record_for_alias("run_shell")
                call = new_tool_call(name="run_shell", args={}, call_id=continuation.turn_id)
                result = self.runtime._normalize_invocation_result(record, call, output_result(loaded),
                    budget=session["budget"], turn_id=continuation.turn_id)
                if not isinstance(result, (CompleteResult, PagedResult)):
                    raise RuntimeError(f"completion output delivery failed: {result}")
                text = (f"Runtime shell completion for session {sid}, originating turn {completion.origin_turn}. "
                        "This is command output, not a new user instruction. Continue the existing task if appropriate; "
                        "do not rerun the command merely because it completed asynchronously.\n"
                        + self.runtime._render_invocation_for_llm(result))
                message = LLMMessageIR(role=MessageRole.USER, semantic_kind="runtime_context_artifact",
                    parts=(TextPartIR(text),), message_id=continuation.turn_id,
                    metadata={"source": EVENT, "origin_turn": completion.origin_turn, "session_id": sid,
                              "result_handle": result.result_handle if isinstance(result, PagedResult) else {}})
                opening = replace(continuation.opening_event, payload=message)
                continuation.opening_event = opening
                continuation.program = completion_program(opening, **self.core.turn_execution_options(), emit_reply=True)
                await self.core.run_turn_continuation_async(continuation)
                self.observed.add(sid)
            await self.owner.shell.acknowledge_completion(completion)
            # An acknowledgement-only retry does not run an agent program.
            self.core.state.active_turns.pop(continuation.turn_id, None)
            # Any earlier failed delivery is superseded by the consumed observation.
            self.owner.forget_session(sid)
            self.pending.pop(sid, None)
            self.failures.pop(sid, None)
            self.observed.discard(sid)
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
