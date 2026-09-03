from __future__ import annotations

from pal.execution.generated_tool_models import (
    BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseInput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseOutput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteInput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteOutput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitInput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitOutput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateInput,
    BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateOutput,
)
from pal.execution.tool_facade import ToolGuidance
from pal.execution.tool_semantics import DIRECT_LOCAL_WRITE, DIRECT_UNSAFE_LOCAL_WRITE

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.service import BehaviorService
from pal.behavior.tools import (
    AffordanceDeleteTool,
    AffordanceSubmitTool,
    AffordanceUpdateTool,
    BehaviorAdviceTool,
)
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution.contracts import CapabilityCall, CapabilityResult
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


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:behavior",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:behavior",
    target_kind="module",
)
@dataclass
class BehaviorIntrospectionProvider:
    service: BehaviorService
    module_id: str = "behavior"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            purpose="Show behavior routing state.",
            use_when="Diagnosing behavior routing — checking affordance count, skill count, or resident prompt budget.",
            do_not_use_when="Looking for specific behavior rules (use advise_behavior). Checking memory state (use recall_memory).",
            failure_next_steps="Read-only diagnostic. If counts look wrong, check behavior repository and skill registry configuration.",
        ),
        aliases=("behavior_show",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        affordances = self.service.repository.list_affordances()
        skill_repository = self.service.skill_repository or self.service.repository.skill_repository
        skills = skill_repository.list_skills()
        structured = {
            "affordance_count": len(affordances),
            "skill_count": len(skills),
            "resident_prompt_budget": self.service.resident_prompt_budget,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="behavior snapshot",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Behavior snapshot", structured),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="advise",
        guidance=ToolGuidance(
            purpose="Ask the behavior router which capabilities, skills, or route guidance may fit the current scenario.",
            use_when="The task route is ambiguous, risky, multi-step, unfamiliar, design/debug/recovery oriented, or the next capability is unclear.",
            do_not_use_when="Current context is sufficient, the user gave a clear direct command, a single capability obviously matches, or the failure is an obvious schema/input mistake. Recalling facts or preferences (use recall_memory).",
            failure_next_steps="Treat results as routing resources, not orders. If router_error is present, fall back to direct capability search with search_tools.",
        ),
        InputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseInput,
        OutputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseOutput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"async_required": True},
        aliases=("advise_behavior",),
    )
    async def advise(self, call: CapabilityCall) -> CapabilityResult:
        return await BehaviorAdviceTool(service=self.service).ainvoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_submit",
        guidance=ToolGuidance(
            purpose="Learn a future behavior rule: when situation X appears, Pal should consider route/action Y.",
            use_when="The user explicitly asks Pal to learn/adopt/follow a future behavior rule or clearly teaches a durable route preference.",
            do_not_use_when="Durable facts, preferences, project context, or repair lessons (use remember_memory). Runtime state. Reusable procedures should be discovered with skill_search or normalized with skill_assimilate.",
            failure_next_steps="If conflict (same scenario exists), default conflict_resolution='ask' returns a user-decision request. Use overwrite or merge to resolve.",
        ),
        InputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitInput,
        OutputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitOutput,
        execution=DIRECT_UNSAFE_LOCAL_WRITE,
        metadata={"canonical_path": "op_behavior_save"},
        aliases=("learn_behavior",),
    )
    def submit_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceSubmitTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_update",
        guidance=ToolGuidance(
            purpose="Update persisted behavior guidance by matching the original behavior text.",
            use_when="Replacing or editing an existing behavior rule's prompt_hint or activation scenario.",
            do_not_use_when="Updating durable facts or preferences (use update_memory). Injected/plugin guidance is read-only here.",
            failure_next_steps="Pass the original rendered guidance line as affordance text, not an internal id. Do not claim success unless the tool confirms.",
        ),
        InputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateInput,
        OutputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateOutput,
        execution=DIRECT_UNSAFE_LOCAL_WRITE,
        aliases=("update_behavior",),
    )
    def update_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceUpdateTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_delete",
        guidance=ToolGuidance(
            purpose="Forget persisted behavior guidance by matching the original behavior text.",
            use_when="The user explicitly asks to remove a specific behavior routing rule.",
            do_not_use_when="Forgetting durable facts or preferences (use forget_memory). Injected/plugin guidance is read-only here.",
            failure_next_steps="Pass the original rendered guidance line as affordance text. If no match, verify the text matches exactly what was rendered.",
        ),
        InputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteInput,
        OutputModel=BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteOutput,
        execution=DIRECT_UNSAFE_LOCAL_WRITE,
        aliases=("forget_behavior",),
    )
    def delete_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceDeleteTool(service=self.service).invoke(call.args)


def register_with_core(context: "MainContext", service: BehaviorService) -> ModuleHandle:
    from pal.behavior.prompt import BehaviorPromptFragmentProvider

    service.execution_runtime = service.execution_runtime or context.execution_runtime
    service.prompt_fragment_registry = service.prompt_fragment_registry or context.prompt_fragment_registry
    provider = BehaviorIntrospectionProvider(service=service)
    prompt_provider = BehaviorPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="behavior",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"behavior": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
