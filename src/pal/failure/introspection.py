from __future__ import annotations

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.failure.runtime import FailureRuntime
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:failure",
    target_kind="module",
)
@dataclass
class FailureIntrospectionProvider:
    runtime: FailureRuntime
    module_id: str = "failure"

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show failure runtime summary")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        return IntrospectionResult(status=RuntimeStatus.OK, text="failure runtime summary", structured=self.runtime.show_summary())

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="recent_reports",
        description="List recent structured failure reports",
    )
    def recent_reports(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = [report.__dict__ for report in self.runtime.recent_reports[-16:]]
        return IntrospectionResult(status=RuntimeStatus.OK, text="recent failure reports", structured={"items": items})


def register_with_core(core, runtime: FailureRuntime) -> ModuleHandle:
    from pal.failure.handler import FailureEventHandler
    from pal.shared import EventKind

    provider = FailureIntrospectionProvider(runtime=runtime)
    handle = ModuleHandle(
        module_id="failure",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"failure": runtime},
    )
    core.context.register_module(handle)
    core.context.event_handler_registry.register(EventKind.REPLY_FAILED, FailureEventHandler(core=core))
    return handle
