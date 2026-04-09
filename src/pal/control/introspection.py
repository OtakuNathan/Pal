from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.control.handler import ControlEventHandler
from pal.control.service import ControlPlane
from pal.core.module_registry import MODULE_TIER_MANAGED_ESSENTIAL, ModuleHandle
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

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class ControlSnapshot:
    deterministic: bool = True
    mounted: bool = True
    degraded: bool = False


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:control",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:control",
    target_kind="module",
)
@dataclass
class ControlIntrospectionProvider:
    control_plane: ControlPlane
    module_id: str = "control"
    mounted: bool = True
    degraded: bool = False

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show control module status",
        aliases=("introspection.module.control.observe",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = ControlSnapshot(deterministic=True, mounted=self.mounted, degraded=self.degraded)
        return IntrospectionResult(status=RuntimeStatus.OK, text="control snapshot", structured=snapshot.__dict__)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach", description="Re-attach control module")
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        return IntrospectionResult(status=RuntimeStatus.OK, text="control re-attached", structured={"mounted": True, "degraded": False})

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach", description="Degrade control module")
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        self.degraded = True
        return IntrospectionResult(status=RuntimeStatus.OK, text="control entered degraded mode", structured={"mounted": False, "degraded": True})


def inspect_control(provider: ControlIntrospectionProvider) -> ControlSnapshot:
    return ControlSnapshot(deterministic=True, mounted=provider.mounted, degraded=provider.degraded)


def register_with_core(context: MainContext, control_plane: ControlPlane) -> ModuleHandle:
    from pal.control.prompt import ControlPromptFragmentProvider

    provider = ControlIntrospectionProvider(control_plane=control_plane)
    prompt_provider = ControlPromptFragmentProvider(provider=provider)
    handle = ModuleHandle(
        module_id="control",
        tier=MODULE_TIER_MANAGED_ESSENTIAL,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        supports_lifecycle_capabilities=True,
        ports={"control": control_plane},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    context.event_handler_registry.register(EventKind.SLASH_COMMAND, ControlEventHandler(control_plane=control_plane))
    context.event_handler_registry.register(EventKind.APPROVAL_REQUEST, ControlEventHandler(control_plane=control_plane))
    return handle
