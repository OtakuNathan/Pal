from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.service import BehaviorService
from pal.behavior.tools import (
    ADVISE_ARGS_SCHEMA,
    ADVISE_RESULT_SCHEMA,
    AFFORDANCE_DELETE_ARGS_SCHEMA,
    AFFORDANCE_DELETE_RESULT_SCHEMA,
    AFFORDANCE_SUBMIT_ARGS_SCHEMA,
    AFFORDANCE_SUBMIT_RESULT_SCHEMA,
    AFFORDANCE_UPDATE_ARGS_SCHEMA,
    AFFORDANCE_UPDATE_RESULT_SCHEMA,
    BEHAVIOR_ADVICE_DESCRIPTION,
    BEHAVIOR_FORGET_DESCRIPTION,
    BEHAVIOR_LEARN_DESCRIPTION,
    BEHAVIOR_UPDATE_DESCRIPTION,
    AffordanceDeleteTool,
    AffordanceSubmitTool,
    AffordanceUpdateTool,
    BehaviorAdviceTool,
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
        description=BEHAVIOR_ADVICE_DESCRIPTION,
        args_schema=ADVISE_ARGS_SCHEMA,
        result_schema=ADVISE_RESULT_SCHEMA,
        metadata={"async_required": True},
    )
    def advise(self, call: CapabilityCall) -> CapabilityResult:
        _ = call
        structured = {"reason": "async_required", "tool": "op_behavior_advise"}
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="behavior_advise requires an active async turn context.",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Behavior advice unavailable", structured),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_submit",
        description=BEHAVIOR_LEARN_DESCRIPTION,
        args_schema=AFFORDANCE_SUBMIT_ARGS_SCHEMA,
        result_schema=AFFORDANCE_SUBMIT_RESULT_SCHEMA,
        metadata={"canonical_path": "op_behavior_save"},
    )
    def submit_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceSubmitTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_update",
        description=BEHAVIOR_UPDATE_DESCRIPTION,
        args_schema=AFFORDANCE_UPDATE_ARGS_SCHEMA,
        result_schema=AFFORDANCE_UPDATE_RESULT_SCHEMA,
    )
    def update_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceUpdateTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="behavior",
        action_name="affordance_delete",
        description=BEHAVIOR_FORGET_DESCRIPTION,
        args_schema=AFFORDANCE_DELETE_ARGS_SCHEMA,
        result_schema=AFFORDANCE_DELETE_RESULT_SCHEMA,
    )
    def delete_affordance(self, call: CapabilityCall) -> CapabilityResult:
        return AffordanceDeleteTool(service=self.service).invoke(call.args)


def register_with_core(context: "MainContext", service: BehaviorService) -> ModuleHandle:
    from pal.behavior.prompt import BehaviorPromptFragmentProvider

    service.execution_runtime = service.execution_runtime or context.execution_runtime
    service.prompt_fragment_registry = service.prompt_fragment_registry or context.prompt_fragment_registry
    context.execution_runtime.register_tool(BehaviorAdviceTool(service=service))
    context.execution_runtime.register_tool(AffordanceSubmitTool(service=service))
    context.execution_runtime.register_tool(AffordanceUpdateTool(service=service))
    context.execution_runtime.register_tool(AffordanceDeleteTool(service=service))
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
