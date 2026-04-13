from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.service.service import ServiceManager, ServiceRunner
from pal.service.source import ServiceEventSource
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class ServiceSnapshot:
    registered_services: int
    pending_triggers: int
    triggered_runs: int
    mounted: bool = True
    degraded: bool = False


@dataclass(frozen=True)
class ServiceTarget:
    service_id: str
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
    scope="service",
    kind="service",
    source="builtin:service",
    target_kind="service",
    iterable_resolver="iter_services",
    target_id_resolver="resolve_service_id",
    target_label_resolver="resolve_service_label",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:service",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:service",
    target_kind="module",
)
@dataclass
class ServiceIntrospectionProvider:
    manager: ServiceManager
    runner: ServiceRunner | None = None
    module_id: str = "service"
    mounted: bool = True
    degraded: bool = False
    refresh_capabilities: Callable[[], None] | None = None

    def iter_services(self) -> list[ServiceTarget]:
        items: list[ServiceTarget] = []
        for definition in sorted(self.manager.registered.values(), key=lambda item: item.service_id):
            items.append(
                ServiceTarget(
                    service_id=definition.service_id,
                    goal=definition.goal,
                    method=definition.method,
                    skill_refs=list(definition.skill_refs),
                    out_channel_id=definition.out_channel_id,
                    enabled=definition.enabled,
                    out_reply_target=dict(definition.out_reply_target),
                    schedule=dict(definition.schedule),
                    next_due_at=self.manager.schedule_engine.next_due_at(definition.service_id),
                    last_run_at=self._last_run_at_for(definition.service_id),
                )
            )
        return items

    def resolve_service_id(self, service: ServiceTarget) -> str:
        return service.service_id

    def resolve_service_label(self, service: ServiceTarget) -> str:
        return service.service_id

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show service module status",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_service(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Service snapshot", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list",
        description="List configured services",
    )
    def list(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = []
        for definition in sorted(self.manager.registered.values(), key=lambda item: item.service_id):
            items.append(
                {
                    "service_id": definition.service_id,
                    "goal": definition.goal,
                    "method": definition.method,
                    "skill_refs": list(definition.skill_refs),
                    "out_channel_id": definition.out_channel_id,
                    "enabled": definition.enabled,
                    "out_reply_target": dict(definition.out_reply_target),
                    "schedule": dict(definition.schedule),
                    "next_due_at": self.manager.schedule_engine.next_due_at(definition.service_id),
                    "last_run_at": self._last_run_at_for(definition.service_id),
                }
            )
        payload = {"items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="configured services",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Configured services", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="service",
        action_name="show",
        description="Show a configured service",
    )
    def show_service(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_service_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                llm_text="service not found",
            )
        payload = {
            "service_id": target.service_id,
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
            text="service snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service snapshot", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="service",
        action_name="last_run",
        description="Show the latest run for a service",
    )
    def last_run(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_service_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                llm_text="service not found",
            )
        repository = self.manager.repository
        if repository is None:
            payload = {"service_id": target.service_id, "run": None}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="service run history unavailable",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Service latest run", payload),
            )
        run = repository.latest_run(target.service_id)
        if run is None:
            payload = {"service_id": target.service_id, "run": None}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="service has not run yet",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Service latest run", payload),
            )
        payload = {"service_id": target.service_id, "run": self._render_run(run)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service latest run",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service latest run", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="service",
        action_name="list_runs",
        description="List recent runs for a service",
        args_schema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["target_id"],
        },
    )
    def list_runs(self, call: IntrospectionCall) -> IntrospectionResult:
        target = self._require_service_target(call)
        if target is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                llm_text="service not found",
            )
        repository = self.manager.repository
        if repository is None:
            payload = {"service_id": target.service_id, "items": []}
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="service run history unavailable",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Service run history", payload),
            )
        limit = max(1, min(50, int(call.args.get("limit") or 10)))
        items = [self._render_run(item) for item in repository.list_runs(target.service_id, limit=limit)]
        payload = {"service_id": target.service_id, "items": items}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service run history",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service run history", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="create",
        description="Create or replace a service definition",
        args_schema={
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "goal": {"type": "string"},
                "method": {"type": "string"},
                "skill_refs": {"type": "array", "items": {"type": "string"}},
                "out_channel_id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "out_reply_target": {"type": "object"},
                "schedule": {"type": "object"},
            },
            "required": ["service_id", "goal"],
        },
    )
    def create(self, call: IntrospectionCall) -> IntrospectionResult:
        service_id = str(call.args.get("service_id") or "").strip()
        goal = str(call.args.get("goal") or "").strip()
        if not service_id or not goal:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="service_id and goal are required",
                llm_text="service_id and goal are required",
            )
        definition = self.manager.create_service(
            service_id=service_id,
            goal=goal,
            method=str(call.args.get("method") or "").strip(),
            skill_refs=[str(item).strip() for item in list(call.args.get("skill_refs") or []) if str(item).strip()],
            out_channel_id=str(call.args.get("out_channel_id") or "").strip() or None,
            schedule=dict(call.args.get("schedule") or {}),
            out_reply_target=dict(call.args.get("out_reply_target") or {}),
            enabled=bool(call.args.get("enabled", True)),
        )
        self._refresh_capabilities()
        payload = {
            "service_id": definition.service_id,
            "out_channel_id": definition.out_channel_id,
            "out_reply_target": dict(definition.out_reply_target),
            "next_due_at": self.manager.schedule_engine.next_due_at(definition.service_id),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service definition created",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service definition created", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="destroy",
        description="Destroy a service definition",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def destroy(self, call: IntrospectionCall) -> IntrospectionResult:
        service_id = str(call.args.get("target_id") or "").strip()
        if not service_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        if not self.manager.destroy_service(service_id):
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                structured={"service_id": service_id},
                llm_text="service not found",
            )
        self._refresh_capabilities()
        payload = {"service_id": service_id}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service destroyed",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service destroyed", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="enable",
        description="Enable a service",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def enable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=True)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="disable",
        description="Disable a service",
        args_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
    )
    def disable(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._set_enabled(call, enabled=False)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_output_channel",
        description="Set or clear the output channel for a service",
        args_schema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "out_channel_id": {"type": "string"},
            },
            "required": ["target_id"],
        },
    )
    def set_output_channel(self, call: IntrospectionCall) -> IntrospectionResult:
        service_id = str(call.args.get("target_id") or "").strip()
        if not service_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        raw_channel = call.args.get("out_channel_id")
        out_channel_id = str(raw_channel or "").strip() or None
        updated = self.manager.set_output_channel(service_id, out_channel_id)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                structured={"service_id": service_id},
                llm_text="service not found",
            )
        self._refresh_capabilities()
        payload = {
            "service_id": service_id,
            "out_channel_id": updated.out_channel_id,
            "out_reply_target": dict(updated.out_reply_target),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service output channel updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service output channel updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_output_target",
        description="Set or clear the output reply target for a service",
        args_schema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "out_reply_target": {"type": "object"},
            },
            "required": ["target_id"],
        },
    )
    def set_output_target(self, call: IntrospectionCall) -> IntrospectionResult:
        service_id = str(call.args.get("target_id") or "").strip()
        if not service_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        out_reply_target = dict(call.args.get("out_reply_target") or {})
        updated = self.manager.set_output_target(service_id, out_reply_target)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                structured={"service_id": service_id},
                llm_text="service not found",
            )
        self._refresh_capabilities()
        payload = {"service_id": service_id, "out_reply_target": dict(updated.out_reply_target)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service output target updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service output target updated", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="update_schedule",
        description="Update the schedule for a service",
        args_schema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "schedule": {"type": "object"},
            },
            "required": ["target_id", "schedule"],
        },
    )
    def update_schedule(self, call: IntrospectionCall) -> IntrospectionResult:
        service_id = str(call.args.get("target_id") or "").strip()
        schedule = call.args.get("schedule")
        if not service_id:
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
        updated = self.manager.update_schedule(service_id, dict(schedule))
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                structured={"service_id": service_id},
                llm_text="service not found",
            )
        self._refresh_capabilities()
        payload = {"service_id": service_id, "next_due_at": self.manager.schedule_engine.next_due_at(service_id)}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service schedule updated",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service schedule updated", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach", description="Attach service module")
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service attached",
            structured={"mounted": True, "degraded": False},
            llm_text=render_titled_structured_for_llm("Service attached", {"mounted": True, "degraded": False}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach", description="Detach service module")
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service detached",
            structured={"mounted": False, "degraded": False},
            llm_text=render_titled_structured_for_llm("Service detached", {"mounted": False, "degraded": False}),
        )

    def _set_enabled(self, call: IntrospectionCall, *, enabled: bool) -> IntrospectionResult:
        service_id = str(call.args.get("target_id") or "").strip()
        if not service_id:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="target_id is required",
                llm_text="target_id is required",
            )
        updated = self.manager.set_enabled(service_id, enabled)
        if updated is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="service not found",
                structured={"service_id": service_id},
                llm_text="service not found",
            )
        self._refresh_capabilities()
        payload = {
            "service_id": service_id,
            "enabled": updated.enabled,
            "next_due_at": self.manager.schedule_engine.next_due_at(service_id),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="service enabled" if enabled else "service disabled",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Service state updated", payload),
        )

    def _require_service_target(self, call: IntrospectionCall) -> ServiceTarget | None:
        service_id = str(call.args.get("target_id") or "").strip()
        if not service_id:
            return None
        for target in self.iter_services():
            if target.service_id == service_id:
                return target
        return None

    def _last_run_at_for(self, service_id: str) -> str | None:
        repository = self.manager.repository
        if repository is None:
            return None
        latest = repository.latest_run(service_id)
        if latest is None:
            return None
        return latest.completed_at or latest.started_at

    def _render_run(self, run) -> dict[str, object]:
        return {
            "service_run_id": run.service_run_id,
            "service_id": run.service_id,
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


def inspect_service(provider: ServiceIntrospectionProvider) -> ServiceSnapshot:
    return ServiceSnapshot(
        registered_services=len(provider.manager.registered),
        pending_triggers=len(provider.manager.pending_triggers),
        triggered_runs=len(provider.runner.triggered) if provider.runner is not None else 0,
        mounted=provider.mounted,
        degraded=provider.degraded,
    )


def register_with_core(
    context: MainContext,
    manager: ServiceManager,
    runner: ServiceRunner | None = None,
) -> ModuleHandle:
    from pal.service.handler import ServiceTriggerHandler

    def refresh_capabilities() -> None:
        handle = context.module_registry.get("service")
        if handle is None or not handle.mounted:
            return
        if handle.mounted_subtree is not None and handle.mounted_subtree.mounted:
            context.execution_runtime.unmount_subtree(handle)
        context.execution_runtime.hydrate_module_handle(handle)
        handle.published_capabilities = context.execution_runtime.mount_subtree(handle)

    manager.on_change = refresh_capabilities
    provider = ServiceIntrospectionProvider(manager=manager, runner=runner, refresh_capabilities=refresh_capabilities)
    source = ServiceEventSource(manager=manager)
    handle = ModuleHandle(
        module_id="service",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[],
        supports_lifecycle_capabilities=True,
        event_sources=[source],
        ports={"service_manager": manager, "service_runner": runner},
    )
    context.register_module(handle)
    context.event_source_registry.attach("service", source)
    context.event_handler_registry.register("service.trigger", ServiceTriggerHandler(manager=manager, runner=runner))
    return handle
