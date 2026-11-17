from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

from pal.control import ControlAction, ControlCommandSpec
from pal.control.runtime_contracts import CORE_CONTROL
from pal.memory.contracts import MEMORY
from pal.core.control_action_registry import ControlActionSpec
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.foundation import EventEnvelope
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.service import DreamingService
from pal.shared.cron import cron_occurrence


class DreamingEventSource:
    source_id = "memory.dreaming"

    def __init__(self, service, timers=None):
        self.service = service
        self.due = None
        self.timers = timers
        self.timer = None
        self.timer_due = None

    def prepare(self, context):
        self.service.reconcile_failure_reminders()
        config = self.service.config
        if not config.enabled or (self.service.task is not None and not self.service.task.done()):
            self.cancel_timer()
            return False
        now = datetime.now(timezone.utc)
        with self.service.storage.connection(write=True) as db:
            row = db.execute("SELECT value FROM memory_settings WHERE key='dreaming_next_due'").fetchone()
            schedule = json.dumps([config.cron, config.timezone])
            previous = db.execute("SELECT value FROM memory_settings WHERE key='dreaming_schedule'").fetchone()
            if row is None or (previous is not None and previous[0] != schedule):
                following = cron_occurrence(config.cron, config.timezone, now).isoformat()
                db.execute("INSERT OR REPLACE INTO memory_settings VALUES ('dreaming_next_due',?)", (following,))
                db.execute("INSERT OR REPLACE INTO memory_settings VALUES ('dreaming_schedule',?)", (schedule,))
                self.due = datetime.fromisoformat(following)
                self.arm_timer()
                return False
            self.due = datetime.fromisoformat(row[0])
        self.arm_timer()
        return self.due <= now

    def drain(self, context):
        now = datetime.now(timezone.utc)
        config = self.service.config
        latest = cron_occurrence(config.cron, config.timezone, now + timedelta(microseconds=1), previous=True)
        self.due = cron_occurrence(config.cron, config.timezone, now)
        # Slot creation and schedule advancement are ordered so a crash can
        # repeat the slot lookup, but cannot lose a scheduled run.
        run_id = self.service.create_run(slot=latest.isoformat())
        with self.service.storage.connection(write=True) as db:
            db.execute("INSERT OR REPLACE INTO memory_settings VALUES ('dreaming_next_due',?)", (self.due.isoformat(),))
        self.arm_timer()
        return [EventEnvelope(event_kind="memory.dreaming", source_kind="memory", payload={"run_id": run_id})]

    def seconds_until_next_due(self):
        if not self.service.config.enabled or (self.service.task is not None and not self.service.task.done()):
            return None
        return max(0.0, (self.due - datetime.now(timezone.utc)).total_seconds()) if self.due else 0.0

    def cancel_timer(self):
        if self.timer is not None:
            self.timer.cancel()
        self.timer = None
        self.timer_due = None

    def arm_timer(self):
        if self.timer is not None and self.timer.pending and self.timer_due == self.due:
            return
        self.cancel_timer()
        delay = self.seconds_until_next_due()
        if self.timers is not None and delay is not None:
            self.timer_due = self.due
            async def wake():
                self.timer = None
                if self.service.on_ready:
                    self.service.on_ready()
            self.timer = self.timers.schedule(delay, wake)


class DreamingEventHandler:
    def __init__(self, service):
        self.service = service

    def can_handle(self, event_kind):
        return event_kind == "memory.dreaming"

    def handle(self, event, context):
        if self.service.config.enabled and self.service.status(event.payload["run_id"])["status"] == "scheduled":
            self.service.start(resume=event.payload["run_id"])
        return []


def register_dreaming(context, provider, llm, runtime_root):
    with ExitStack() as cleanup:
        return _register_dreaming(context, provider, llm, runtime_root, cleanup)


def _register_dreaming(context, provider, llm, runtime_root, cleanup):
    control_api = context.require_port(CORE_CONTROL)
    timers = control_api.open_timer_scope()
    cleanup.callback(timers.close)
    storage = provider.repository.catalog
    service = DreamingService(storage=storage, provider=provider, llm=llm,
                              config=DreamingConfig.load(runtime_root, storage=storage),
                              host=control_api, memory=context.require_port(MEMORY))
    # Publication is already reconciled by opening catalog.current at startup.
    # Interrupted unpublished work keeps its cache but relinquishes admission.
    service.recover_interrupted_runs()
    service.on_ready = control_api.notify_ready
    source = DreamingEventSource(service, timers)
    handler = DreamingEventHandler(service)

    def command(invocation):
        return ControlAction(action_kind="memory_dreaming", target_scope="memory", route=invocation.route,
                             args={"argv": list(invocation.argv), "raw_text": invocation.raw_text})

    def control(action):
        argv = list(action.args.get("argv") or ["status"])
        operation = argv[0]
        if control_api.snapshot().memory_maintenance and operation not in {"status", "report", "config", "configure", "enable", "disable"}:
            return {"message": "Dreaming is already running. You can inspect its status or change the configuration for the next run."}
        if operation in {"configure", "enable", "disable"}:
            try:
                raw = action.args.get("raw_text", "").split(None, 2)
                material = raw[2] if len(raw) == 3 else " ".join(argv[1:])
                changes = json.loads(material) if operation == "configure" else {"enabled": operation == "enable"}
                result = service.configure(changes)
            except (ValueError, TypeError) as exc:
                return {"message": f"Invalid dreaming configuration: {exc}"}
        elif operation == "config":
            result = service.status()["current_configuration"]
        elif operation in {"status", "report"}:
            result = service.status(argv[1] if len(argv) > 1 else None)
        elif operation == "start":
            result = service.start(dry_run="--dry-run" in argv)
        elif operation == "resume":
            run_id = argv[1] if len(argv) > 1 else service.status().get("run_id")
            result = service.start(resume=run_id)
        else:
            return {"message": "/dreaming status | report [run_id] | start [--dry-run] | resume [run_id] | config | configure <JSON> | enable | disable"}
        return {"message": json.dumps(result, ensure_ascii=False, indent=2)}

    async def execute_control(action: ControlAction) -> None:
        await control_api.reply(action, control(action)["message"])

    handle = ModuleHandle(module_id="memory.dreaming", tier=MODULE_TIER_DETACHABLE, detachable=True,
        ports={"dreaming": service}, event_sources=[source], event_handlers={"memory.dreaming": [handler]},
        control_action_handlers={"memory_dreaming": ControlActionSpec(execute_control, allow_during_maintenance=True)},
        control_commands=(ControlCommandSpec(name="dreaming", handler=command,
            description="Inspect or start conservative memory duplicate consolidation.",
            usage="/dreaming status | report | start [--dry-run] | resume | config | configure <JSON> | enable | disable"),),
        shutdown_async=service.shutdown, timer_scope=timers)
    context.register_module(handle)
    cleanup.callback(context.unregister_module, handle)
    context.event_source_registry.attach("memory.dreaming", source)
    context.event_handler_registry.register("memory.dreaming", handler, owner=handle, module_id="memory.dreaming")
    cleanup.pop_all()
    return handle
