from __future__ import annotations

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.core.runtime import PalCore
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    EventKind,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm


@dataclass(frozen=True)
class CoreSnapshot:
    queued_events: int
    active_turns: int
    mode: str
    detached_modules: list[str]


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:core",
    target_kind="module",
)
@dataclass
class CoreIntrospectionProvider:
    core: PalCore
    module_id: str = "core"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="observe",
        description="Observe module-level state for core",
    )
    def observe(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_core(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="core snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Core snapshot", snapshot.__dict__),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="configure",
        description="Configure module-level state for core",
        args_schema={
            "type": "object",
            "properties": {"mode": {"type": "string"}},
        },
    )
    def configure(self, call: IntrospectionCall) -> IntrospectionResult:
        mode = call.args.get("mode")
        if mode is not None:
            self.core.state.mode = str(mode)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="core configuration updated",
            structured={"mode": self.core.state.mode},
            llm_text=render_titled_structured_for_llm("Core configuration updated", {"mode": self.core.state.mode}),
        )


def inspect_core(provider: CoreIntrospectionProvider) -> CoreSnapshot:
    core = provider.core
    return CoreSnapshot(
        queued_events=len(core.main_loop.queue),
        active_turns=len(core.state.active_turns),
        mode=core.state.mode,
        detached_modules=sorted(core.state.detached_modules),
    )


def register_with_core(core: PalCore) -> ModuleHandle:
    from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider
    from pal.core.turn_handler import TurnEventHandler

    provider = CoreIntrospectionProvider(core=core)
    prompt_provider = MinimalOperatingRulesPromptFragmentProvider()
    handle = ModuleHandle(
        module_id="core",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"core": core},
    )
    core.context.register_module(handle)
    core.context.prompt_fragment_registry.register(prompt_provider)
    core.context.event_handler_registry.register(EventKind.USER_MESSAGE, TurnEventHandler(core=core))
    return handle
