"""Deliver background package observations to Pal, never directly to a channel."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import threading

from pal.foundation import EventEnvelope
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.shared.result_rendering import render_structured_for_llm

EVENT = "packages.job.completed"
MODULE = "packages.completions"


def completion_program(event, *, core_mode, max_output_tokens):
    from pal.core.turns import L1CommitPayload, MailboxReplyEffect, agent_turn_program
    from pal.memory import L1MessageKind, L1TranscriptMessage
    from pal.shared import PromptAssemblyContext

    def commit(final_reply, observations, replies):
        return L1CommitPayload(turn_id=event.event_id, tool_observations=list(observations), transcript=[
            L1TranscriptMessage(role="user", content=event.payload.text, kind=L1MessageKind.RUNTIME_CONTEXT_ARTIFACT),
            *[L1TranscriptMessage(role="assistant", content=text, kind=L1MessageKind.ASSISTANT_REPLY)
              for text in (replies or [final_reply]) if text],
        ])

    return (yield from agent_turn_program(
        turn_id=event.event_id,
        build_assembly_context=lambda frame: PromptAssemblyContext(
            event=event, core_mode=core_mode,
            metadata={"retry_note": frame.retry_note} if frame.retry_note else {}),
        render_final_text=lambda outcome: str(outcome.text or "") if outcome else "",
        build_commit_payload=commit, max_output_tokens=max_output_tokens,
        emit_mid_text=lambda text: MailboxReplyEffect(text=text, terminal=False, stream_companion=True),
        emit_final_text=lambda text: MailboxReplyEffect(text=text),
    ))


class PackageCompletionSource:
    source_id = MODULE

    def __init__(self, context):
        self.context = context
        self.pending = {}
        self.claimed = set()
        self.seen = set()
        self.tasks = set()
        self.lock = threading.Lock()
        self.closed = False
        self.registered = False

    @property
    def core(self):
        return self.context.port_registry.get("core:core")

    def notifier(self, turn_id):
        core = self.core
        opening = core.state.active_turns.get(turn_id) if core is not None else None
        binding = getattr(opening, "delivery_binding", None)
        memory = self.context.port_registry.get("memory:memory")
        if self.closed or binding is None or not callable(getattr(memory, "begin_l1_turn", None)):
            return None
        if not self.registered:
            self.context.event_source_registry.attach(MODULE, self)
            self.context.event_handler_registry.register(EVENT, self, module_id=MODULE)
            self.registered = True

        def notify(state):
            with self.lock:
                if self.closed:
                    raise RuntimeError("Package completion delivery is stopping")
                job_id = state["job_id"]
                if job_id in self.seen:
                    return
                self.seen.add(job_id)
                observation = {key: value for key, value in state.items()
                               if key not in {"notification", "notification_error", "next_step"}}
                self.pending[job_id] = (deepcopy(observation), binding, turn_id)
            core.notify_ready()
        return notify

    def _idle(self):
        state = self.core.state
        return not (state.resident_quiescing or state.memory_maintenance or state.active_turns or state.pending_channel_turns
                    or any(not task.done() for task in state.turn_tasks.values()))

    def prepare(self, context):
        with self.lock:
            return not self.closed and self._idle() and any(key not in self.claimed for key in self.pending)

    def drain(self, context):
        with self.lock:
            if self.closed or not self._idle():
                return []
            job_id = next((key for key in self.pending if key not in self.claimed), None)
            if job_id is None:
                return []
            self.claimed.add(job_id)
        return [EventEnvelope(event_kind=EVENT, source_kind="packages", payload=job_id)]

    def can_handle(self, event_kind):
        return event_kind == EVENT

    async def handle(self, event, context):
        from pal.core.turns import TurnContinuation
        core = self.core
        job_id = event.payload
        async with core.state.channel_turn_transition_lock:
            with self.lock:
                if self.closed or job_id not in self.claimed or job_id not in self.pending:
                    return []
                if not self._idle():
                    self.claimed.discard(job_id)
                    return []
                state, binding, opening_turn = self.pending[job_id]
            identity = f"package:{job_id}:completed"
            text = (
                "<runtime_context_update>Background package operation completed. This is an observation, "
                "not a new user request. Continue unfinished work from the initiating task within its existing "
                "authorization. Do not repeat the operation. Inspect the actual activation result; "
                "completion alone does not mean activation succeeded.</runtime_context_update>\n"
                + render_structured_for_llm({"opening_turn_id": opening_turn, **state})
            )
            message = LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),),
                                  message_id=identity, semantic_kind="runtime_context_artifact")
            opening = EventEnvelope(event_kind=EVENT, source_kind="packages", payload=message, event_id=identity)
            continuation = TurnContinuation(turn_id=identity, opening_event=opening, delivery_binding=binding,
                program=completion_program(opening, **core.turn_execution_options()), correlation_id=opening_turn)
            task = asyncio.create_task(self._run(job_id, continuation))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            core.state.active_turn_id = identity
            core.state.resident_drained_event.clear()
            core.track_turn_task(continuation, task)
        return []

    async def _run(self, job_id, continuation):
        core = self.core
        try:
            memory = self.context.port_registry["memory:memory"]
            core._begin_tool_result_turn(continuation)
            memory.begin_l1_turn(continuation.turn_id, user_message=continuation.opening_event.payload)
            await core.run_turn_continuation_async(continuation)
            return "success"
        except asyncio.CancelledError:
            core.turn_manager.cleanup_interrupted(continuation.turn_id, reason="interrupted")
            raise
        except Exception as exc:
            core.turn_manager.cleanup_interrupted(continuation.turn_id, reason="failed")
            core.state.diagnostics.append({"kind": EVENT + ".failed", "job_id": job_id,
                                           "error": f"{type(exc).__name__}: {exc}"})
            return "failed"
        finally:
            # A failed model continuation is not a reason to replay effects.
            with self.lock:
                self.pending.pop(job_id, None)
                self.claimed.discard(job_id)
            core.state.active_turns.pop(continuation.turn_id, None)
            core.turn_manager._mark_turn_exited(continuation.turn_id)
            if not self.closed:
                await core._start_next_queued_turn_async()
            core.notify_ready()

    def close(self):
        with self.lock:
            self.closed = True
            self.pending.clear()
            self.claimed.clear()
        if self.registered:
            self.context.event_source_registry.detach_module(MODULE)
            self.context.event_handler_registry.detach_module(MODULE)
        for task in tuple(self.tasks):
            task.get_loop().call_soon_threadsafe(task.cancel)
