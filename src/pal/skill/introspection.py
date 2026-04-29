from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall
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
from pal.skill.service import SkillService
from pal.skill.tools import (
    SKILL_ASSIMILATE_ARGS_SCHEMA,
    SKILL_COMMIT_ARGS_SCHEMA,
    SKILL_DISABLE_ARGS_SCHEMA,
    SKILL_INJECT_ARGS_SCHEMA,
    SKILL_INJECT_RESULT_SCHEMA,
    SKILL_READ_ARGS_SCHEMA,
    SKILL_SEARCH_ARGS_SCHEMA,
    SKILL_UPDATE_ARGS_SCHEMA,
    SkillAssimilateTool,
    SkillCommitTool,
    SkillDisableTool,
    SkillInjectTool,
    SkillReadTool,
    SkillSearchTool,
    SkillUpdateTool,
)

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:skill",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:skill",
    target_kind="module",
)
@dataclass
class SkillIntrospectionProvider:
    service: SkillService
    module_id: str = "skill"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show skill management state",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        skills = self.service.repository.list_skills()
        by_status: dict[str, int] = {}
        for skill in skills:
            by_status[skill.status] = by_status.get(skill.status, 0) + 1
        structured = {
            "skill_count": len(skills),
            "active_skill_count": len([skill for skill in skills if skill.active]),
            "status_counts": by_status,
            "pending_candidate_count": len(self.service.pending_candidates),
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="skill snapshot",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill snapshot", structured),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="assimilate",
        description="Create a sanitized Pal skill candidate from plain text or SKILL.md content. Does not commit.",
        args_schema=SKILL_ASSIMILATE_ARGS_SCHEMA,
        result_schema={"type": "object"},
        metadata={"llm_exposed": True, "async_required": True},
    )
    def assimilate(self, call: CapabilityCall):
        return SkillAssimilateTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="commit",
        description="Commit a sanitized skill candidate and its thin affordance.",
        args_schema=SKILL_COMMIT_ARGS_SCHEMA,
        result_schema={"type": "object"},
        metadata={"llm_exposed": True},
    )
    def commit(self, call: CapabilityCall):
        return SkillCommitTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="update",
        description="Update a normalized skill and refresh its thin affordance.",
        args_schema=SKILL_UPDATE_ARGS_SCHEMA,
        result_schema={"type": "object"},
        metadata={"llm_exposed": True},
    )
    def update(self, call: CapabilityCall):
        return SkillUpdateTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="disable",
        description="Disable a normalized skill without deleting history.",
        args_schema=SKILL_DISABLE_ARGS_SCHEMA,
        result_schema={"type": "object"},
        metadata={"llm_exposed": True},
    )
    def disable(self, call: CapabilityCall):
        return SkillDisableTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="search",
        description="Search normalized Pal skills for the current scenario or explicit skill name. Does not inject manuals.",
        args_schema=SKILL_SEARCH_ARGS_SCHEMA,
        result_schema={"type": "object"},
        metadata={"llm_exposed": True},
    )
    def search(self, call: CapabilityCall):
        return SkillSearchTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="read",
        description="Read normalized Pal skill metadata, optionally including manual text.",
        args_schema=SKILL_READ_ARGS_SCHEMA,
        result_schema={"type": "object"},
        metadata={"llm_exposed": True},
    )
    def op_read(self, call: CapabilityCall):
        return SkillReadTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="inject",
        description="Inject a registered active skill manual as a tool observation without executing capabilities.",
        args_schema=SKILL_INJECT_ARGS_SCHEMA,
        result_schema=SKILL_INJECT_RESULT_SCHEMA,
        metadata={"llm_exposed": True},
    )
    def inject(self, call: CapabilityCall):
        return SkillInjectTool(service=self.service).invoke(call.args)


def register_with_core(context: "MainContext", service: SkillService) -> ModuleHandle:
    from pal.skill.prompt import SkillPromptFragmentProvider

    context.execution_runtime.register_tool(SkillAssimilateTool(service=service))
    context.execution_runtime.register_tool(SkillCommitTool(service=service))
    context.execution_runtime.register_tool(SkillUpdateTool(service=service))
    context.execution_runtime.register_tool(SkillDisableTool(service=service))
    context.execution_runtime.register_tool(SkillSearchTool(service=service))
    context.execution_runtime.register_tool(SkillReadTool(service=service))
    context.execution_runtime.register_tool(SkillInjectTool(service=service))
    provider = SkillIntrospectionProvider(service=service)
    prompt_provider = SkillPromptFragmentProvider()
    handle = ModuleHandle(
        module_id="skill",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"skill": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
