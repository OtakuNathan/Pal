from __future__ import annotations

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.core.runtime import PalCore
from pal.execution.generated_tool_models import (
    CoreCapabilitiesCoreIntrospectionProviderConfigureCacheWarmDeadlineInput,
    CoreCapabilitiesCoreIntrospectionProviderConfigureInput,
)
from pal.execution.tool_facade import ToolGuidance
from pal.execution.tool_semantics import INDIRECT_LOCAL_WRITE
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
        guidance=ToolGuidance(
            purpose="Observe core runtime state — queued events, active turns, mode, detached modules.",
            use_when="Diagnosing core health: event backlog, stuck turns, or checking which modules are detached.",
            do_not_use_when="Checking control plane status (use control_show). Checking execution tool count (use exec_show).",
            failure_next_steps="Read-only diagnostic. If event queue is backed up, turns may be stuck. If modules are unexpectedly detached, investigate lifecycle.",
        ),
        aliases=("core_observe",),
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
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="configure",
        guidance=ToolGuidance(
            purpose="Configure core runtime mode.",
            use_when="Switching core operating mode (e.g. normal, maintenance).",
            do_not_use_when="Reading core state (use core_observe). Configuring a specific module (use that module's capabilities).",
            failure_next_steps="If mode change fails, check core_observe for current state and module health.",
        ),
        InputModel=CoreCapabilitiesCoreIntrospectionProviderConfigureInput,
        aliases=("core_configure",),
        execution=INDIRECT_LOCAL_WRITE,
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

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="cache_warm_deadline",
        guidance=ToolGuidance(
            purpose="Inspect the hot prompt-cache compact reminder configuration and current in-memory deadline.",
            use_when="Checking whether Pal will suggest compacting before the confirmed A cache expires.",
            do_not_use_when="Checking general token usage (use llm_usage). Triggering compaction now (use the compact control action).",
            failure_next_steps="If no timer is scheduled, inspect whether A is confirmed, the provider exposes an A TTL, and the prefix exceeds the configured minimum.",
        ),
        aliases=("core_cache_warm_deadline",),
    )
    def cache_warm_deadline(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = self.core.cache_warm_deadline.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="cache warm deadline status",
            structured=snapshot,
            llm_text=render_titled_structured_for_llm(
                "Cache warm deadline",
                snapshot,
            ),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="configure_cache_warm_deadline",
        guidance=ToolGuidance(
            purpose="Enable, disable, or tune Pal's compact reminder before the confirmed A prompt cache expires.",
            use_when="The user asks to change hot-cache compact reminders, their lead time, or their minimum prompt size.",
            do_not_use_when="Triggering compaction immediately. Inspect first when the requested setting is ambiguous.",
            failure_next_steps="Use core_cache_warm_deadline to inspect valid current values; lead_seconds must be at least 30 and min_prefix_tokens at least 1024.",
        ),
        InputModel=CoreCapabilitiesCoreIntrospectionProviderConfigureCacheWarmDeadlineInput,
        aliases=("core_configure_cache_warm_deadline",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def configure_cache_warm_deadline(
        self,
        call: IntrospectionCall,
    ) -> IntrospectionResult:
        try:
            snapshot = self.core.cache_warm_deadline.configure(
                enabled=(
                    bool(call.args["enabled"])
                    if call.args.get("enabled") is not None
                    else None
                ),
                lead_seconds=(
                    int(call.args["lead_seconds"])
                    if call.args.get("lead_seconds") is not None
                    else None
                ),
                min_prefix_tokens=(
                    int(call.args["min_prefix_tokens"])
                    if call.args.get("min_prefix_tokens") is not None
                    else None
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text=str(exc),
                llm_text=str(exc),
            )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="cache warm deadline configuration updated",
            structured=snapshot,
            llm_text=render_titled_structured_for_llm(
                "Cache warm deadline configuration updated",
                snapshot,
            ),
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
    from pal.core.control_handler import CoreControlActionHandler
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
        control_action_handlers={
            "cache_warm_deadline_ignore": core.handle_cache_warm_deadline_ignore,
            "cache_warm_deadline_disable": core.handle_cache_warm_deadline_disable,
        },
        ports={"core": core},
        shutdown_sync=core.close,
    )
    core.context.register_module(handle)
    core.context.prompt_fragment_registry.register(prompt_provider)
    core.context.event_handler_registry.register(EventKind.USER_MESSAGE, TurnEventHandler(core=core))
    core.context.event_handler_registry.register(EventKind.CONTROL_ACTION, CoreControlActionHandler(core=core))
    return handle
