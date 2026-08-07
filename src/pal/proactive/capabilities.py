from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderDeleteInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderDisableInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderEnableInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderListRunsInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputChannelInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputTargetInput,
    ProactiveCapabilitiesProactiveIntrospectionProviderUpdateScheduleInput,
)

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.proactive.runtime import ProactiveManager, ProactiveRunner
from pal.proactive.source import ProactiveEventSource
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    EventKind,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


PROACTIVE_MODULE_ID = "proactive"
_PROACTIVE_SCHEDULE_CADENCES = {"manual", "cron", "once"}


@dataclass(frozen=True)
class ProactiveSnapshot:
    registered_proactive_tasks: int
    pending_triggers: int
    triggered_runs: int
    mounted: bool = True
    degraded: bool = False


@dataclass(frozen=True)
class ProactiveTarget:
    proactive_id: str
    goal: str
    method: str
    skill_refs: list[str]
    out_channel_id: str | None
    enabled: bool
    out_reply_target: dict[str, object]
    schedule: dict[str, object]
    next_due_at: str | None
    last_run_at: str | None


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="proactive",
    kind="proactive_task",
    source="builtin:proactive",
    target_kind="proactive_task",
    iterable_resolver="iter_proactive_tasks",
    target_id_resolver="resolve_proactive_id",
    target_label_resolver="resolve_proactive_label",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:proactive",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:proactive",
    target_kind="module",
)
@dataclass
class ProactiveIntrospectionProvider:
    manager: ProactiveManager
    runner: ProactiveRunner | None = None
    module_id: str = PROACTIVE_MODULE_ID
    mounted: bool = True
    degraded: bool = False
    refresh_capabilities: Callable[[], None] | None = None

    def iter_proactive_tasks(self) -> list[ProactiveTarget]:
        items: list[ProactiveTarget] = []
        for definition in sorted(self.manager.registered.values(), key=lambda item: item.proactive_id):
            items.append(
                ProactiveTarget(
                    proactive_id=definition.proactive_id,
                    goal=definition.goal,
                    method=definition.method,
                    skill_refs=list(definition.skill_refs),
                    out_channel_id=definition.out_channel_id,
                    enabled=definition.enabled,
                    out_reply_target=dict(definition.out_reply_target),
                    schedule=dict(definition.schedule),
                    next_due_at=self.manager.schedule_engine.next_due_at(definition.proactive_id),
                    last_run_at=self._last_run_at_for(definition.proactive_id),
                )
            )
        return items

    def resolve_proactive_id(self, target: ProactiveTarget) -> str:
        return target.proactive_id

    def resolve_proactive_label(self, target: ProactiveTarget) -> str:
        return target.proactive_id

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show proactive module status for scheduled, reminder, recurring, or push tasks",
        guidance=ToolGuidance(purpose="Show proactive module status for scheduled, reminder, recurring, or push tasks"),
        aliases=("proactive_show",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_proactive(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Proactive snapshot", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        description="List configured proactive tasks such as reminders, scheduled jobs, recurring reports, and push notifications",
        guidance=ToolGuidance(purpose="List configured proactive tasks such as reminders, scheduled jobs, recurring reports, and push notifications"),
        aliases=("proactive_list",),
    )
    def list(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = []
        for definition in sorted(self.manager.registered.values(), key=lambda item: item.proactive_id):
            items.append(
                {
                    "proactive_id": definition.proactive_id,
                    "goal": definition.goal,
                    "method": definition.method,
                    "skill_refs": list(definition.skill_refs),
                    "out_channel_id": definition.out_channel_id,
                    "enabled": definition.enabled,
                    "out_reply_target": dict(definition.out_reply_target),
                    "schedule": dict(definition.schedule),
                    "next_due_at": self.manager.schedule_engine.next_due_at(definition.proactive_id),
                    "last_run_at": self._last_run_at_for(definition.proactive_id),
                }
            )
        payload = {"items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="configured proactive tasks",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Configured proactive tasks", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="proactive",
        action_name="show",
        description="Show a configured proactive task",
        guidance=ToolGuidance(purpose="Show a configured proactive task"),
        aliases=("proactive_show",),
    )
    def show_task(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_proactive_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                llm_text="proactive task not found",
            )
        payload = {
            "proactive_id": target.proactive_id,
            "goal": target.goal,
            "method": target.method,
            "skill_refs": list(target.skill_refs),
            "out_channel_id": target.out_channel_id,
            "out_reply_target": dict(target.out_reply_target),
            "enabled": target.enabled,
            "schedule": dict(target.schedule),
            "next_due_at": target.next_due_at,
            "last_run_at": target.last_run_at,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive task snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive task snapshot", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="proactive",
        action_name="last_run",
        description="Show the latest run for a proactive task",
        guidance=ToolGuidance(purpose="Show the latest run for a proactive task"),
        aliases=("proactive_last_run",),
    )
    def last_run(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_proactive_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                llm_text="proactive task not found",
            )
        repository = self.manager.repository
        if repository is None:
            payload = {"proactive_id": target.proactive_id, "run": None}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="proactive run history unavailable",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Proactive latest run", payload),
            )
        run = repository.latest_run(target.proactive_id)
        if run is None:
            payload = {"proactive_id": target.proactive_id, "run": None}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="proactive task has not run yet",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Proactive latest run", payload),
            )
        payload = {"proactive_id": target.proactive_id, "run": self._render_run(run)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive latest run",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive latest run", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="proactive",
        action_name="list_runs",
        description="List recent runs for a proactive task",
        guidance=ToolGuidance(purpose="List recent runs for a proactive task"),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderListRunsInput,
        aliases=("proactive_list_runs",),
    )
    def list_runs(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_proactive_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                llm_text="proactive task not found",
            )
        repository = self.manager.repository
        if repository is None:
            payload = {"proactive_id": target.proactive_id, "items": []}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="proactive run history unavailable",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Proactive run history", payload),
            )
        limit = max(1, min(50, int(call.args.get("limit") or 10)))
        items = [self._render_run(item) for item in repository.list_runs(target.proactive_id, limit=limit)]
        payload = {"proactive_id": target.proactive_id, "items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive run history",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive run history", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="create",
        description="Create or replace a proactive task for future work.",
        guidance=ToolGuidance(
            purpose="Create or replace a proactive task for future work.",
            use_when="For one-time reminders, scheduled jobs, recurring reports, periodic checks, or push notifications.",
            do_not_use_when="Not for one-shot immediate tasks (handle directly).",
            failure_next_steps="Correct invalid schedule/cron syntax; verify the output channel exists.",
        ),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput,
        aliases=("proactive_create",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def create(self, call: IntrospectionCall) -> IntrospectionResult:
        proactive_id = str(call.args.get("proactive_id") or "").strip()
        goal = str(call.args.get("goal") or "").strip()
        if not proactive_id or not goal:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="proactive_id and goal are required",
                llm_text="proactive_id and goal are required",
            )
        skill_refs_raw = call.args.get("skill_refs") or []
        if not isinstance(skill_refs_raw, list):
            return _invalid_result("skill_refs must be an array")
        out_reply_target_raw = call.args.get("out_reply_target") or {}
        if not isinstance(out_reply_target_raw, dict):
            return _invalid_result("out_reply_target must be an object")
        enabled_raw = call.args.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            return _invalid_result("enabled must be a boolean")
        schedule, invalid = _normalize_schedule_argument(call.args.get("schedule"), required=False)
        if invalid is not None:
            return invalid
        definition = self.manager.create_task(
            proactive_id=proactive_id,
            goal=goal,
            method=str(call.args.get("method") or "").strip(),
            skill_refs=[str(item).strip() for item in skill_refs_raw if str(item).strip()],
            out_channel_id=str(call.args.get("out_channel_id") or "").strip() or None,
            schedule=schedule,
            out_reply_target=dict(out_reply_target_raw),
            enabled=enabled_raw,
        )
        self._refresh_capabilities()
        payload = {
            "proactive_id": definition.proactive_id,
            "out_channel_id": definition.out_channel_id,
            "out_reply_target": dict(definition.out_reply_target),
            "next_due_at": self.manager.schedule_engine.next_due_at(definition.proactive_id),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive task created",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive task created", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="delete",
        description="Delete (destroy) a proactive task",
        guidance=ToolGuidance(purpose="Delete (destroy) a proactive task"),
        aliases=("proactive_delete",),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderDeleteInput,
        execution=INDIRECT_LOCAL_WRITE,
    )
    def delete(self, call: IntrospectionCall) -> IntrospectionResult:
        proactive_id = str(call.args.get("target_id") or "").strip()
        if not proactive_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        if not self.manager.destroy_task(proactive_id):
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                structured={"proactive_id": proactive_id},
                llm_text="proactive task not found",
            )
        self._refresh_capabilities()
        payload = {"proactive_id": proactive_id}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive task deleted",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive task deleted", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="enable",
        description="Enable a proactive task",
        guidance=ToolGuidance(purpose="Enable a proactive task"),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderEnableInput,
        aliases=("proactive_enable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="disable",
        description="Disable a proactive task",
        guidance=ToolGuidance(purpose="Disable a proactive task"),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderDisableInput,
        aliases=("proactive_disable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_output_channel",
        description="Set or clear the output channel for a proactive task",
        guidance=ToolGuidance(purpose="Set or clear the output channel for a proactive task"),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputChannelInput,
        aliases=("proactive_set_output_channel",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_output_channel(self, call: IntrospectionCall) -> IntrospectionResult:
        proactive_id = str(call.args.get("target_id") or "").strip()
        if not proactive_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        raw_channel = call.args.get("out_channel_id")
        out_channel_id = str(raw_channel or "").strip() or None
        updated = self.manager.set_output_channel(proactive_id, out_channel_id)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                structured={"proactive_id": proactive_id},
                llm_text="proactive task not found",
            )
        self._refresh_capabilities()
        payload = {
            "proactive_id": proactive_id,
            "out_channel_id": updated.out_channel_id,
            "out_reply_target": dict(updated.out_reply_target),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive output channel updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive output channel updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_output_target",
        description="Set or clear the output reply target for a proactive task",
        guidance=ToolGuidance(purpose="Set or clear the output reply target for a proactive task"),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputTargetInput,
        aliases=("proactive_set_output_target",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_output_target(self, call: IntrospectionCall) -> IntrospectionResult:
        proactive_id = str(call.args.get("target_id") or "").strip()
        if not proactive_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        out_reply_target_raw = call.args.get("out_reply_target") or {}
        if not isinstance(out_reply_target_raw, dict):
            return _invalid_result("out_reply_target must be an object")
        out_reply_target = dict(out_reply_target_raw)
        updated = self.manager.set_output_target(proactive_id, out_reply_target)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                structured={"proactive_id": proactive_id},
                llm_text="proactive task not found",
            )
        self._refresh_capabilities()
        payload = {"proactive_id": proactive_id, "out_reply_target": dict(updated.out_reply_target)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive output target updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive output target updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="update_schedule",
        description="Update the schedule for a proactive task",
        guidance=ToolGuidance(purpose="Update the schedule for a proactive task"),
        InputModel=ProactiveCapabilitiesProactiveIntrospectionProviderUpdateScheduleInput,
        aliases=("proactive_update_schedule",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def update_schedule(self, call: IntrospectionCall) -> IntrospectionResult:
        proactive_id = str(call.args.get("target_id") or "").strip()
        schedule = call.args.get("schedule")
        if not proactive_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        if not isinstance(schedule, dict):
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="schedule must be an object",
                llm_text="schedule must be an object",
            )
        normalized_schedule, invalid = _normalize_schedule_argument(schedule, required=True)
        if invalid is not None:
            return invalid
        updated = self.manager.update_schedule(proactive_id, normalized_schedule)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                structured={"proactive_id": proactive_id},
                llm_text="proactive task not found",
            )
        self._refresh_capabilities()
        payload = {"proactive_id": proactive_id, "next_due_at": self.manager.schedule_engine.next_due_at(proactive_id)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive schedule updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive schedule updated", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach", description="Attach proactive module",
        guidance=ToolGuidance(purpose="Attach proactive module"), aliases=("proactive_attach",), execution=INDIRECT_CONTROL)
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive attached",
            structured={"mounted": True, "degraded": False},
            llm_text=render_titled_structured_for_llm("Proactive attached", {"mounted": True, "degraded": False}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach", description="Detach proactive module",
        guidance=ToolGuidance(purpose="Detach proactive module"), aliases=("proactive_detach",), execution=INDIRECT_CONTROL)
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive detached",
            structured={"mounted": False, "degraded": False},
            llm_text=render_titled_structured_for_llm("Proactive detached", {"mounted": False, "degraded": False}),
        )

    def _set_enabled(self, call: IntrospectionCall, *, enabled: bool) -> IntrospectionResult:
        proactive_id = str(call.args.get("target_id") or "").strip()
        if not proactive_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        updated = self.manager.set_enabled(proactive_id, enabled)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="proactive task not found",
                structured={"proactive_id": proactive_id},
                llm_text="proactive task not found",
            )
        self._refresh_capabilities()
        payload = {
            "proactive_id": proactive_id,
            "enabled": updated.enabled,
            "next_due_at": self.manager.schedule_engine.next_due_at(proactive_id),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="proactive task enabled" if enabled else "proactive task disabled",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Proactive state updated", payload),
        )

    def _require_proactive_target(self, call: IntrospectionCall) -> ProactiveTarget | None:
        proactive_id = str(call.args.get("target_id") or "").strip()
        if not proactive_id:
            return None
        for target in self.iter_proactive_tasks():
            if target.proactive_id == proactive_id:
                return target
        return None

    def _last_run_at_for(self, proactive_id: str) -> str | None:
        repository = self.manager.repository
        if repository is None:
            return None
        latest = repository.latest_run(proactive_id)
        if latest is None:
            return None
        return latest.completed_at or latest.started_at

    def _render_run(self, run) -> dict[str, object]:
        return {
            "proactive_run_id": run.proactive_run_id,
            "proactive_id": run.proactive_id,
            "trigger_kind": run.trigger_kind,
            "status": run.status,
            "trigger_metadata": dict(run.trigger_metadata or {}),
            "turn_id": run.turn_id,
            "output_summary": run.output_summary,
            "error_text": run.error_text,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        }

    def _refresh_capabilities(self) -> None:
        if self.refresh_capabilities is None:
            return
        self.refresh_capabilities()


def _normalize_schedule_argument(raw: object, *, required: bool) -> tuple[dict[str, object], IntrospectionResult | None]:
    if raw is None:
        if required:
            return {}, _invalid_result("schedule is required")
        return {"cadence": "manual", "timezone": "UTC"}, None
    if not isinstance(raw, dict):
        return {}, _invalid_result("schedule must be an object")

    cadence = str(raw.get("cadence") or "manual").strip().lower()
    if cadence not in _PROACTIVE_SCHEDULE_CADENCES:
        return {}, _invalid_result(
            "schedule.cadence must be manual, cron, or once",
            structured={"cadence": cadence},
        )

    timezone_name = str(raw.get("timezone") or "UTC").strip() or "UTC"
    try:
        timezone_info = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return {}, _invalid_result(
            "schedule.timezone must be a valid IANA timezone",
            structured={"timezone": timezone_name},
        )

    normalized: dict[str, object] = {"cadence": cadence, "timezone": timezone_name}
    if cadence == "manual":
        return normalized, None

    if cadence == "cron":
        cron_expr = str(raw.get("cron") or "").strip()
        if not cron_expr:
            return {}, _invalid_result("schedule.cron is required when cadence is cron")
        try:
            import croniter

            croniter.croniter(cron_expr, datetime.now(timezone_info))
        except Exception:
            return {}, _invalid_result(
                "schedule.cron must be a valid 5-field cron expression",
                structured={"cron": cron_expr},
            )
        normalized["cron"] = cron_expr
        return normalized, None

    run_at_raw = str(raw.get("run_at_utc") or "").strip()
    if not run_at_raw:
        return {}, _invalid_result("schedule.run_at_utc is required when cadence is once")
    try:
        parsed = datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return {}, _invalid_result(
            "schedule.run_at_utc must be an ISO datetime",
            structured={"run_at_utc": run_at_raw},
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_info)
    target = parsed.astimezone(timezone.utc)
    if target <= datetime.now(timezone.utc):
        return {}, _invalid_result(
            "schedule.run_at_utc must be in the future",
            structured={"run_at_utc": run_at_raw},
        )
    normalized["run_at_utc"] = target.isoformat()
    return normalized, None


def _invalid_result(text: str, *, structured: dict[str, object] | None = None) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.INVALID,
        text=text,
        structured=dict(structured or {}),
        llm_text=text,
    )


def inspect_proactive(provider: ProactiveIntrospectionProvider) -> ProactiveSnapshot:
    return ProactiveSnapshot(
        registered_proactive_tasks=len(provider.manager.registered),
        pending_triggers=len(provider.manager.pending_triggers),
        triggered_runs=len(provider.runner.triggered) if provider.runner is not None else 0,
        mounted=provider.mounted,
        degraded=provider.degraded,
    )


def register_with_core(
    context: MainContext,
    manager: ProactiveManager,
    runner: ProactiveRunner | None = None,
) -> ModuleHandle:
    from pal.proactive.handler import ProactiveTriggerHandler
    from pal.proactive.prompt import ProactivePromptFragmentProvider

    def refresh_capabilities() -> None:
        handle = context.module_registry.get(PROACTIVE_MODULE_ID)
        if handle is None or not handle.mounted:
            return
        if handle.mounted_subtree is not None and handle.mounted_subtree.mounted:
            context.execution_runtime.unmount_subtree(handle)
        context.execution_runtime.hydrate_module_handle(handle)
        handle.published_capabilities = context.execution_runtime.mount_subtree(handle)

    manager.on_change = refresh_capabilities
    provider = ProactiveIntrospectionProvider(manager=manager, runner=runner, refresh_capabilities=refresh_capabilities)
    prompt_provider = ProactivePromptFragmentProvider(manager=manager)
    source = ProactiveEventSource(manager=manager)
    event_handler = ProactiveTriggerHandler(manager=manager, runner=runner)
    handle = ModuleHandle(
        module_id=PROACTIVE_MODULE_ID,
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        supports_lifecycle_capabilities=True,
        event_sources=[source],
        event_handlers={EventKind.PROACTIVE_TRIGGER: [event_handler]},
        ports={"proactive_manager": manager, "proactive_runner": runner},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    context.event_source_registry.attach(PROACTIVE_MODULE_ID, source)
    context.event_handler_registry.register(EventKind.PROACTIVE_TRIGGER, event_handler, module_id=PROACTIVE_MODULE_ID)
    return handle
