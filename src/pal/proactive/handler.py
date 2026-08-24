from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.proactive.runtime import ProactiveManager, ProactiveRunner
from pal.proactive.turns import build_proactive_turn_continuation, settled_output_text
from pal.shared import EventKind, ProactiveTriggerEvent


@dataclass
class ProactiveTriggerHandler(EventHandler):
    manager: ProactiveManager
    runner: ProactiveRunner | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.PROACTIVE_TRIGGER

    def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        if not isinstance(event.payload, ProactiveTriggerEvent):
            return []
        definition = self.manager.registered.get(event.payload.proactive_id)
        if definition is None:
            return []
        core = context.require_port("core:core")
        options = core.turn_execution_options()
        continuation = build_proactive_turn_continuation(
            context,
            event.payload,
            definition,
            core_mode=str(options.get("core_mode") or "default"),
            max_output_tokens=int(options.get("max_output_tokens") or 1024),
        )
        task = asyncio.create_task(self._run_trigger_async(core, event.payload, continuation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        core.track_turn_task(continuation, task)
        return []

    async def _run_trigger_async(self, core, trigger: ProactiveTriggerEvent, continuation) -> str:
        proactive_run_id = self.runner.begin_run(trigger) if self.runner is not None else None
        try:
            outcome = await core.run_turn_continuation_async(continuation)
        except asyncio.CancelledError:
            core.turn_manager.cleanup_interrupted(continuation.turn_id, reason="interrupted")
            if self.runner is not None:
                self.runner.fail_run(proactive_run_id, error_text="interrupted")
            raise
        except Exception as exc:
            core.turn_manager.cleanup_interrupted(continuation.turn_id, reason="failed")
            core.state.diagnostics.append(
                {
                    "kind": "proactive.trigger.failed",
                    "turn_id": continuation.turn_id,
                    "proactive_id": trigger.proactive_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            if self.runner is not None:
                self.runner.fail_run(proactive_run_id, error_text=str(exc))
            return "failed"
        if self.runner is not None:
            self.runner.complete_run(proactive_run_id, turn_id=outcome.turn_id, final_reply=settled_output_text(outcome))
        self.manager.mark_run_completed(trigger.proactive_id)
        return "success"
