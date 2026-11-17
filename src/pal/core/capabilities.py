from __future__ import annotations

from dataclasses import dataclass, field

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.core.runtime import PalCore
from pal.memory.persistence.contracts import RESIDENT_HISTORY
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
    scheduler: dict = field(default_factory=dict)


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
            search_terms=('status', 'inspect', 'show', 'get', 'check', 'summary', 'queue', 'backlog'),
            purpose="Inspect core runtime state, queued events, active turns, detached modules.",
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


def inspect_core(provider: CoreIntrospectionProvider) -> CoreSnapshot:
    core = provider.core
    return CoreSnapshot(
        queued_events=len(core.main_loop.queue),
        active_turns=len(core.state.active_turns),
        mode=core.state.mode,
        detached_modules=sorted(core.state.detached_modules),
        scheduler=core.state.scheduler.snapshot(),
    )


def register_with_core(core: PalCore) -> ModuleHandle:
    from pal.core.commands import action_handlers, command_specs, interaction_specs
    from pal.control.runtime_contracts import CORE_CONTROL
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
        control_action_handlers=action_handlers(core),
        control_commands=command_specs(),
        control_interactions=interaction_specs(),
        ports={"core": core, "control": core.control_runtime},
        port_contracts=(CORE_CONTROL,),
        shutdown_sync=core.close,
    )
    core.context.register_module(handle)
    core.context.prompt_fragment_registry.register(prompt_provider)
    core.context.event_handler_registry.register(EventKind.USER_MESSAGE, TurnEventHandler(core=core), owner=handle)
    core.context.event_handler_registry.register(EventKind.CONTROL_ACTION, CoreControlActionHandler(core=core), owner=handle)
    return handle
