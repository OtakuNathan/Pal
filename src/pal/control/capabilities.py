from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
)
from pal.execution.tool_facade import ToolGuidance

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
            failure_next_steps="Read-only diagnostic. If degraded, use control_attach to recover.",
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

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="attach",
        guidance=ToolGuidance(
            purpose="Re-attach control module — recover from degraded mode.",
            use_when="The control plane was degraded (via control_detach) and needs to resume normal operation.",
            do_not_use_when="Attaching a channel endpoint (use channel_attach). The control module is already mounted.",
            failure_next_steps="No external dependencies. If still degraded after attach, check control_show.",
        ), aliases=("control_attach",), execution=INDIRECT_CONTROL)
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="control re-attached",
            structured={"mounted": True, "degraded": False},
            llm_text=render_titled_structured_for_llm("Control re-attached", {"mounted": True, "degraded": False}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="lifecycle", action_name="detach",
        guidance=ToolGuidance(
            purpose="Degrade control module — enter degraded mode where the control plane stops active management.",
            use_when="Rarely needed. Only when explicitly instructed to put the control plane into degraded mode.",
            do_not_use_when="Detaching a channel endpoint (use channel_detach). Normal operation — degrading control is disruptive.",
            failure_next_steps="Recover with control_attach when ready to resume normal operation.",
        ), aliases=("control_detach",), execution=INDIRECT_CONTROL)
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        self.degraded = True
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="control entered degraded mode",
            structured={"mounted": False, "degraded": True},
            llm_text=render_titled_structured_for_llm("Control entered degraded mode", {"mounted": False, "degraded": True}),
        )


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
    context.event_handler_registry.register(EventKind.INTERACTION_RESULT, ControlEventHandler(control_plane=control_plane))
    return handle
