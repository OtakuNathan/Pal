from __future__ import annotations

from dataclasses import dataclass

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.core.runtime import PalCore
from pal.core.resident_history import RESIDENT_HISTORY
from pal.execution.generated_tool_models import (
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
from pal.shared.inspection import inspection_snapshot


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
@capability_node(
    namespace=OPERATION_NAMESPACE, scope="module", kind="module",
    source="builtin:core", target_kind="module",
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
            search_objects=('state', 'states', 'task', 'tasks', 'turn', 'turns'),
            search_terms=('status', 'inspect', 'show', 'get', 'check', 'summary', 'cache', 'deadline', 'reminder', 'queue', 'backlog'),
            purpose="Inspect core runtime state, queued events, active turns, detached modules and the prompt-cache reminder deadline together.",
            use_when='Diagnosing core health: event backlog, stuck turns, or checking which modules are detached. Queue backlog and active turns are current workload observations. The mode label alone does not prove maintenance or draining; defer activation while Pal is busy.',
            do_not_use_when="Checking control plane status (use pal control status). Checking execution tool count (use inspect_execution_state).",
            failure_next_steps="Read-only diagnostic. If event queue is backed up, turns may be stuck. If modules are unexpectedly detached, investigate lifecycle.",
        ),
        aliases=("observe_core",),
    )
    def observe(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_core(self)
        history = self.core.context.port_registry.optional(RESIDENT_HISTORY)
        payload = inspection_snapshot(snapshot.__dict__, sections={
            "cache_reminder": lambda: self.core.cache_warm_deadline.inspect(),
            "resident_history": lambda: history.inspect() if history is not None else {"status": "unavailable"},
        })
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="core snapshot",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Core snapshot", payload),
        )

    def configure(self, call: IntrospectionCall) -> IntrospectionResult:
        mode = call.args.get("mode")
        if mode is not None:
            self.core.state.mode = str(mode)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="core mode label",
            structured={"mode": self.core.state.mode},
            llm_text=render_titled_structured_for_llm("Core mode label", {"mode": self.core.state.mode}),
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

    # Configuration maintenance is exposed through the official Pal CLI.
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
