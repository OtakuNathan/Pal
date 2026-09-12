from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pal.control import ControlAction, ControlCommandSpec
from pal.core.module_registry import ModuleHandle, MODULE_TIER_MANAGED_ESSENTIAL
from pal.foundation import EventEnvelope
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.service import DreamingService
from pal.shared.cron import cron_occurrence


class DreamingEventSource:
    source_id = "memory.dreaming"

    def __init__(self, service):
        self.service = service
        self.due = None

    def prepare(self, context):
        config = self.service.config
        if not config.enabled or (self.service.task is not None and not self.service.task.done()):
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
                return False
            self.due = datetime.fromisoformat(row[0])
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
        return [EventEnvelope(event_kind="memory.dreaming", source_kind="memory", payload={"run_id": run_id})]

    def seconds_until_next_due(self):
        if not self.service.config.enabled or (self.service.task is not None and not self.service.task.done()):
            return None
        return max(0.0, (self.due - datetime.now(timezone.utc)).total_seconds()) if self.due else 0.0


class DreamingEventHandler:
    def __init__(self, service):
        self.service = service

    def can_handle(self, event_kind):
        return event_kind == "memory.dreaming"

    def handle(self, event, context):
        if self.service.status(event.payload["run_id"])["status"] == "scheduled":
            self.service.start(resume=event.payload["run_id"])
        return []


def register_dreaming(core, provider, llm, runtime_root):
    storage = provider.repository.catalog
    service = DreamingService(storage=storage, provider=provider, llm=llm,
                              config=DreamingConfig.load(runtime_root), core=core)
    # Publication is already reconciled by opening catalog.current at startup.
    # Interrupted unpublished work keeps its cache but relinquishes admission.
    with storage.connection(write=True) as db:
        db.execute("UPDATE dreaming_runs SET status='failed' WHERE status NOT IN ('completed','failed')")
    service.on_ready = core.notify_ready
    source = DreamingEventSource(service)
    handler = DreamingEventHandler(service)

    def command(invocation):
        return ControlAction(action_kind="memory_dreaming", target_scope="memory", route=invocation.route,
                             args={"argv": list(invocation.argv)})

    def control(action):
        argv = list(action.args.get("argv") or ["status"])
        operation = argv[0]
        if core.state.memory_maintenance and operation not in {"status", "report"}:
            return {"message": "Dreaming 正在运行；可使用 status 或 report。"}
        if operation in {"status", "report"}:
            result = service.status(argv[1] if len(argv) > 1 else None)
        elif operation == "start":
            result = service.start(dry_run="--dry-run" in argv)
        elif operation == "resume":
            run_id = argv[1] if len(argv) > 1 else service.status().get("run_id")
            result = service.start(resume=run_id)
        else:
            return {"message": "/dreaming status | report [run_id] | start [--dry-run] | resume [run_id]"}
        return {"message": json.dumps(result, ensure_ascii=False, indent=2)}

    core.context.register_module(ModuleHandle(module_id="memory.dreaming", tier=MODULE_TIER_MANAGED_ESSENTIAL,
        ports={"dreaming": service}, event_sources=[source], event_handlers={"memory.dreaming": [handler]},
        control_action_handlers={"memory_dreaming": control}, shutdown_async=service.shutdown))
    core.context.event_source_registry.attach("memory.dreaming", source)
    core.context.event_handler_registry.register("memory.dreaming", handler, module_id="memory.dreaming")
    core.context.require_port("control:control").register_command(ControlCommandSpec(name="dreaming", handler=command,
        description="Inspect or start conservative memory duplicate consolidation.",
        usage="/dreaming status | report | start [--dry-run] | resume"))
    return service
