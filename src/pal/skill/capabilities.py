from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.decorators import affordance
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
from pal.skill.builtin_skills import (
    PAL_CHANNEL_PROVIDER_DEVELOPMENT_SKILL_ID,
    PAL_LLM_ADAPTER_ENDPOINT_DEVELOPMENT_SKILL_ID,
    PAL_PLUGIN_DEVELOPMENT_SKILL_ID,
    builtin_declared_skills,
)
from pal.skill.contracts import SkillDescriptor
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
@affordance(
    affordance_id="declared.skill.pal_plugin_development",
    title="Pal plugin development skill",
    scenario_text=(
        "The user wants to create, repair, review, or hot-refresh a Pal plugin, "
        "plugin capability, build_plugin entrypoint, ModuleHandle surface, or plugin lifecycle."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.plugin.development` before designing, "
        "writing, repairing, or attaching Pal plugin code."
    ),
    activation_terms=(
        "pal plugin",
        "plugin development",
        "create plugin",
        "repair plugin",
        "hot refresh plugin",
        "build_plugin",
        "ModuleHandle",
        "capability extension",
        "插件开发",
        "写插件",
        "修插件",
    ),
    skill_refs=(PAL_PLUGIN_DEVELOPMENT_SKILL_ID,),
    priority=35,
    activation_threshold=0.2,
    metadata={"skill_trigger": True, "resident": False},
)
@affordance(
    affordance_id="declared.skill.pal_llm_adapter_endpoint_development",
    title="Pal LLM adapter endpoint development skill",
    scenario_text=(
        "The user wants to add, repair, test, or validate an LLM provider adapter, OpenAI-compatible "
        "serialization adapter, runtime-root adapter source, or matching llm_endpoints row."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.llm.adapter_endpoint.development` before "
        "creating adapter code or endpoint metadata. Do not refresh/load the running runtime unless the user explicitly asks."
    ),
    activation_terms=(
        "llm adapter",
        "llm endpoint",
        "provider adapter",
        "endpoint adapter",
        "runtime adapter",
        "OpenAI-compatible adapter",
        "new model provider",
        "add llm provider",
        "llm/adapters",
        "llm_endpoints",
        "适配器",
        "模型 endpoint",
        "模型端点",
    ),
    skill_refs=(PAL_LLM_ADAPTER_ENDPOINT_DEVELOPMENT_SKILL_ID,),
    priority=35,
    activation_threshold=0.2,
    metadata={"skill_trigger": True, "resident": False, "requires_user_refresh": True},
)
@affordance(
    affordance_id="declared.skill.pal_channel_provider_development",
    title="Pal channel provider development skill",
    scenario_text=(
        "The user wants to add, repair, test, or hot-load a Pal channel provider, channel endpoint, "
        "runtime-root channel provider manifest, slash-command path, inline interaction rendering, or channel lifecycle."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.channel.provider.development` before "
        "creating provider.toml, channel provider code, endpoint metadata, or channel interaction handling."
    ),
    activation_terms=(
        "channel provider",
        "channel endpoint",
        "channel integration",
        "new channel",
        "add channel",
        "runtime channel provider",
        "channel/providers",
        "provider.toml",
        "ChannelEndpointProviderManager",
        "FactoryChannelProvider",
        "ChannelEndpointQueueBase",
        "slash command",
        "inline keyboard",
        "频道",
        "通道",
        "channel 接入",
    ),
    skill_refs=(PAL_CHANNEL_PROVIDER_DEVELOPMENT_SKILL_ID,),
    priority=35,
    activation_threshold=0.2,
    metadata={"skill_trigger": True, "resident": False, "runtime_root_layout": "channel/providers"},
)
@dataclass
class SkillIntrospectionProvider:
    service: SkillService
    module_id: str = "skill"

    def declared_skills(self) -> tuple[SkillDescriptor, ...]:
        return builtin_declared_skills(module_id=self.module_id)

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
        metadata={"async_required": True},
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
    )
    def read(self, call: CapabilityCall):
        return SkillReadTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="inject",
        description="Inject a registered active skill manual as a tool observation without executing capabilities.",
        args_schema=SKILL_INJECT_ARGS_SCHEMA,
        result_schema=SKILL_INJECT_RESULT_SCHEMA,
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
