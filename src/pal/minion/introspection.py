from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.minion.runtime import MinionRuntime
from pal.minion.service import TaskingService
from pal.minion.source import MinionEventSource
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


# ── minion runtime introspection ──────────────────────────────────────

@dataclass(frozen=True)
class MinionSnapshot:
    accepted_contexts: int


class MinionIntrospection(Protocol):
    def snapshot(self) -> MinionSnapshot:
        ...


def inspect_minion(runtime: MinionRuntime) -> MinionSnapshot:
    return MinionSnapshot(accepted_contexts=len(runtime.accepted_contexts))


WorkerSnapshot = MinionSnapshot
WorkerIntrospection = MinionIntrospection
inspect_worker = inspect_minion


# ── tasking module introspection ──────────────────────────────────────

@dataclass(frozen=True)
class TaskingSnapshot:
    mounted: bool = True
    degraded: bool = False


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:tasking",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:tasking",
    target_kind="module",
)
@dataclass
class TaskingIntrospectionProvider:
    service: TaskingService
    module_id: str = "tasking"
    mounted: bool = True
    degraded: bool = False

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show tasking module status",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_tasking(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="tasking snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Tasking snapshot", snapshot.__dict__),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach", description="Attach tasking module")
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="tasking attached",
            structured={"mounted": True, "degraded": False},
            llm_text=render_titled_structured_for_llm("Tasking attached", {"mounted": True, "degraded": False}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach", description="Detach tasking module")
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="tasking detached",
            structured={"mounted": False, "degraded": False},
            llm_text=render_titled_structured_for_llm("Tasking detached", {"mounted": False, "degraded": False}),
        )


def inspect_tasking(provider: TaskingIntrospectionProvider) -> TaskingSnapshot:
    return TaskingSnapshot(
        mounted=provider.mounted,
        degraded=provider.degraded,
    )


def register_with_core(context: MainContext, service: TaskingService) -> ModuleHandle:
    provider = TaskingIntrospectionProvider(service=service)
    source = MinionEventSource(service=service)
    handle = ModuleHandle(
        module_id="tasking",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[],
        supports_lifecycle_capabilities=True,
        event_sources=[source],
        ports={"tasking": service},
    )
    context.register_module(handle)
    context.event_source_registry.attach("tasking", source)
    return handle
