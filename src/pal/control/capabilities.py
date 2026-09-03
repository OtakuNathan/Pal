from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
)
from pal.execution.tool_facade import ToolGuidance

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.control.handler import ControlEventHandler
from pal.control.service import ControlPlane
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
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
        guidance=ToolGuidance(
            purpose="Show control module status.",
            use_when="Diagnosing whether the control plane is mounted or in degraded mode.",
            do_not_use_when="Checking core runtime state (use core_observe). Checking execution tool count (use exec_show).",
            failure_next_steps="Read-only diagnostic. If control must be restarted, use plugin_attach with name=control.",
        ),
        aliases=("control_show",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = ControlSnapshot(deterministic=True, mounted=self.mounted, degraded=self.degraded)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="control snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Control snapshot", snapshot.__dict__),
        )


def inspect_control(provider: ControlIntrospectionProvider) -> ControlSnapshot:
    return ControlSnapshot(deterministic=True, mounted=provider.mounted, degraded=provider.degraded)


def register_with_core(context: MainContext, control_plane: ControlPlane) -> ModuleHandle:
    from pal.control.prompt import ControlPromptFragmentProvider

    provider = ControlIntrospectionProvider(control_plane=control_plane)
    prompt_provider = ControlPromptFragmentProvider(provider=provider)
    event_handler = ControlEventHandler(control_plane=control_plane)
    handle = ModuleHandle(
        module_id="control",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        event_handlers={
            EventKind.SLASH_COMMAND: [event_handler],
            EventKind.INTERACTION_RESULT: [event_handler],
        },
        ports={"control": control_plane},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    context.event_handler_registry.register(EventKind.SLASH_COMMAND, event_handler, module_id="control")
    context.event_handler_registry.register(EventKind.INTERACTION_RESULT, event_handler, module_id="control")
    return handle
