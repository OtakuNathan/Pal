from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.foundation.diagnostics import exception_report
from pal.proactive.runtime import ProactiveManager, ProactiveRunner
from pal.proactive.turns import build_proactive_turn_continuation, settled_output_text
from pal.shared import EventKind, ProactiveTriggerEvent


@dataclass
class ProactiveTriggerHandler(EventHandler):
    manager: ProactiveManager
    runner: ProactiveRunner | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)
    lifecycle_gate: object | None = None

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.PROACTIVE_TRIGGER

    def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        if not isinstance(event.payload, ProactiveTriggerEvent):
            return []
        definition = self.manager.registered.get(event.payload.proactive_id)
        if definition is None:
            return []
        core = context.require_port("core:core")
        try:
            continuation = self._build_continuation(core, event.payload, definition)
        except Exception as exc:
            run_id = None
            if self.runner is not None:
                try:
                    run_id = self.runner.begin_run(event.payload)
                except Exception as recording_exc:
                    exc.add_note("Creating the failed run record also failed:\n" + exception_report(recording_exc))
            self._record_failure(core, event.payload, None, run_id, exc)
            return []
        task = asyncio.create_task(self._run_trigger_async(core, event.payload, continuation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Register before returning to the event loop, which uses tracked tasks
        # to decide whether there is still work to await.
        core.track_turn_task(continuation, task)
        return []

    @staticmethod
    def _build_continuation(core, trigger, definition):
        options = core.turn_execution_options()
        return build_proactive_turn_continuation(
            core.context,
            trigger,
            definition,
            core_mode=str(options.get("core_mode") or "default"),
            max_output_tokens=int(options.get("max_output_tokens") or 1024),
        )

    async def _run_trigger_async(self, core, trigger: ProactiveTriggerEvent, continuation) -> str:
        gate = self.lifecycle_gate or core.context.execution_runtime.lifecycle_gate
        async with gate.read_async():
            return await self._run_trigger_guarded_async(core, trigger, continuation)

    async def _run_trigger_guarded_async(self, core, trigger: ProactiveTriggerEvent, continuation) -> str:
        proactive_run_id = None
        try:
            proactive_run_id = self.runner.begin_run(trigger) if self.runner is not None else None
            outcome = await core.run_turn_continuation_async(continuation)
            self.manager.mark_run_completed(trigger.proactive_id)
            if self.runner is not None:
                self.runner.complete_run(proactive_run_id, turn_id=outcome.turn_id, final_reply=settled_output_text(outcome))
        except asyncio.CancelledError as exc:
            self._record_failure(core, trigger, continuation, proactive_run_id, exc, interrupted=True)
            raise
        except Exception as exc:
            self._record_failure(core, trigger, continuation, proactive_run_id, exc)
            return "failed"
        return "success"

    def _record_failure(self, core, trigger, continuation, run_id, exc, *, interrupted=False) -> None:
        errors = [("interrupted: " if interrupted else "") + exception_report(exc)]
        turn_id = continuation.turn_id if continuation is not None else ""
        if continuation is not None:
            try:
                core.turn_manager.cleanup_interrupted(turn_id, reason="interrupted" if interrupted else "failed")
            except Exception as cleanup_exc:
                errors.append("Turn cleanup also failed:\n" + exception_report(cleanup_exc))
        if self.runner is not None:
            result_count = len(self.runner.results)
            try:
                self.runner.fail_run(run_id, error_text="\n\n".join(errors))
            except Exception as recording_exc:
                errors.append("Recording the failure also failed:\n" + exception_report(recording_exc))
            # Keep an inspectable fallback even if the history database is unavailable.
            if len(self.runner.results) == result_count:
                self.runner.results.append({})
            self.runner.results[-1].update(proactive_run_id=run_id, proactive_id=trigger.proactive_id,
                turn_id=turn_id, status="failed", error_text="\n\n".join(errors))
        core.state.diagnostics.append({"kind": "proactive.trigger.failed", "turn_id": turn_id,
            "proactive_id": trigger.proactive_id, "proactive_run_id": run_id, "error": "\n\n".join(errors)})
