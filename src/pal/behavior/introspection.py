from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.service import BehaviorService
from pal.behavior.tools import (
    ADVISE_ARGS_SCHEMA,
    ADVISE_RESULT_SCHEMA,
    AFFORDANCE_SUBMIT_ARGS_SCHEMA,
    AFFORDANCE_SUBMIT_RESULT_SCHEMA,
    AffordanceSubmitTool,
    SKILL_INJECT_ARGS_SCHEMA,
    SKILL_INJECT_RESULT_SCHEMA,
    BehaviorAdviceTool,
    SkillInjectTool,
)
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
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
    scope="skill",
    kind="module",
    source="builtin:behavior",
    target_kind="module",
    path_module_id="skill",
)
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
        description="Show behavior routing state",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        affordances = self.service.repository.list_affordances()
        skills = self.service.repository.list_skills()
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
        description="Ask the behavior router for scenario-to-action route candidates. Async tool path is required.",
        args_schema=ADVISE_ARGS_SCHEMA,
        result_schema=ADVISE_RESULT_SCHEMA,
        metadata={"llm_exposed": True, "async_required": True},
    )
    def advise(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        structured = {"reason": "async_required", "tool": "op_behavior_advise"}
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="op_behavior_advise requires async execution.",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Behavior advice unavailable", structured),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_submit",
        description="Persist a user-instructed or learned affordance. Do not use for ordinary memory cases.",
        args_schema=AFFORDANCE_SUBMIT_ARGS_SCHEMA,
        result_schema=AFFORDANCE_SUBMIT_RESULT_SCHEMA,
        metadata={"llm_exposed": True},
    )
    def submit_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceSubmitTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="skill",
        family="skill",
        action_name="inject",
        description="Inject a registered skill manual as a tool observation without executing capabilities.",
        args_schema=SKILL_INJECT_ARGS_SCHEMA,
        result_schema=SKILL_INJECT_RESULT_SCHEMA,
        metadata={"llm_exposed": True},
    )
    def inject_skill(self, call: CapabilityCall) -> CapabilityResult:
        return SkillInjectTool(service=self.service).invoke(call.args)


def register_with_core(context: "MainContext", service: BehaviorService) -> ModuleHandle:
    from pal.behavior.prompt import BehaviorPromptFragmentProvider

    service.execution_runtime = service.execution_runtime or context.execution_runtime
    context.execution_runtime.register_tool(BehaviorAdviceTool(service=service))
    context.execution_runtime.register_tool(SkillInjectTool(service=service))
    context.execution_runtime.register_tool(AffordanceSubmitTool(service=service))
    provider = BehaviorIntrospectionProvider(service=service)
    prompt_provider = BehaviorPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="behavior",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"behavior": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
