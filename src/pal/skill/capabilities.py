from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_WRITE,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    SkillCapabilitiesSkillIntrospectionProviderAssimilateInput,
    SkillCapabilitiesSkillIntrospectionProviderAssimilateOutput,
    SkillCapabilitiesSkillIntrospectionProviderCommitInput,
    SkillCapabilitiesSkillIntrospectionProviderCommitOutput,
    SkillCapabilitiesSkillIntrospectionProviderDisableInput,
    SkillCapabilitiesSkillIntrospectionProviderDisableOutput,
    SkillCapabilitiesSkillIntrospectionProviderInjectInput,
    SkillCapabilitiesSkillIntrospectionProviderInjectOutput,
    SkillCapabilitiesSkillIntrospectionProviderReadInput,
    SkillCapabilitiesSkillIntrospectionProviderReadOutput,
    SkillCapabilitiesSkillIntrospectionProviderSearchInput,
    SkillCapabilitiesSkillIntrospectionProviderSearchOutput,
    SkillCapabilitiesSkillIntrospectionProviderUpdateInput,
    SkillCapabilitiesSkillIntrospectionProviderUpdateOutput,
)

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.decorators import affordance
from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.contracts import CapabilityCall
from pal.execution.tool_semantics import INDIRECT_LOCAL_READ
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
    PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_SKILL_ID,
    PAL_PLUGIN_DEVELOPMENT_SKILL_ID,
    builtin_declared_skills,
)
from pal.skill.contracts import SkillDescriptor
from pal.skill.service import SkillService
from pal.skill.tools import (
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
    affordance_id="declared.skill.pal_llm_model_hook_endpoint_development",
    title="Pal LLM model-hook endpoint development skill",
    scenario_text=(
        "The user wants to add, repair, test, or validate an exact-model request hook "
        "or matching llm_endpoints row."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.llm.model_hook_endpoint.development` before "
        "creating model-hook code or endpoint metadata. Do not refresh/load the running runtime unless the user explicitly asks."
    ),
    activation_terms=(
        "llm model hook",
        "llm endpoint",
        "model-specific instruction",
        "endpoint hook",
        "runtime model hook",
        "new model provider",
        "add llm provider",
        "llm/models",
        "llm_endpoints",
        "适配器",
        "模型 endpoint",
        "模型端点",
    ),
    skill_refs=(PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_SKILL_ID,),
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
        guidance=ToolGuidance(
            purpose="Show skill management state.",
            use_when="Diagnosing skill system health — how many skills exist, how many are active, pending candidate count.",
            do_not_use_when="Searching for a specific skill (use skill_search). Checking behavior routing rules (use behavior_show). Checking memory state (use memory_show).",
            failure_next_steps="Read-only diagnostic. If no skills are active, use skill_search to find relevant ones.",
        ),
        aliases=("skill_show",),
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
        guidance=ToolGuidance(
            purpose="Create a sanitized skill candidate from plain text or SKILL.md content without committing.",
            use_when="The user provides a reusable procedure, playbook, or domain manual that should become a normalized skill.",
            do_not_use_when="Recording a durable fact or preference (use remember_memory). Learning a routing rule (use learn_behavior). The content is a one-off procedure not worth normalizing.",
            failure_next_steps="Review the candidate output and use skill_commit to persist it. If assimilation may have succeeded but its result was lost, inspect skill_show before re-assimilating; do not create duplicate pending candidates blindly.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderAssimilateInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderAssimilateOutput,
        metadata={"async_required": True},
        aliases=("skill_assimilate",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    async def assimilate(self, call: CapabilityCall):
        args = dict(call.args)
        desired_name = str(args.pop("desired_name", "") or "").strip()
        if desired_name:
            args["desired_skill_id"] = desired_name
        return await SkillAssimilateTool(service=self.service).ainvoke(args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="commit",
        guidance=ToolGuidance(
            purpose="Commit a sanitized skill candidate and register its thin behavior affordance.",
            use_when="After skill_assimilate produced a candidate you've reviewed and want to persist as a normalized skill.",
            do_not_use_when="Committing unreviewed candidates. Writing a durable fact (use remember_memory).",
            failure_next_steps="If validation fails before commit, fix the candidate fields. If commit may have succeeded, reconcile with skill_search using the skill name before retrying. If the skill already exists, use skill_update instead.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderCommitInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderCommitOutput,
        aliases=("skill_commit",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    def commit(self, call: CapabilityCall):
        return SkillCommitTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="update",
        guidance=ToolGuidance(
            purpose="Update a normalized skill's metadata or manual text and refresh its affordance.",
            use_when="Editing an existing skill's content, activation terms, or metadata.",
            do_not_use_when="Updating a durable fact (use update_memory). Updating a behavior rule (use update_behavior). Creating a new skill (use skill_assimilate + skill_commit).",
            failure_next_steps="If the skill name is not found, verify it with skill_search. Changes take effect on next scenario match.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderUpdateInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderUpdateOutput,
        aliases=("skill_update",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def update(self, call: CapabilityCall):
        return SkillUpdateTool(service=self.service).invoke(_skill_name_args(call.args))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="disable",
        guidance=ToolGuidance(
            purpose="Disable a normalized skill so it stops matching scenarios, without deleting its history.",
            use_when="A skill is no longer relevant or is producing false-positive activations.",
            do_not_use_when="Forgetting a durable fact (use forget_memory). Removing a behavior rule (use forget_behavior). Permanently deleting skill data (this only disables).",
            failure_next_steps="If the skill name is not found, verify it with skill_search. Re-enable by using skill_update to set status back to active.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderDisableInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderDisableOutput,
        aliases=("skill_disable",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def disable(self, call: CapabilityCall):
        return SkillDisableTool(service=self.service).invoke(_skill_name_args(call.args))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="search",
        guidance=ToolGuidance(
            purpose="Search normalized skills by scenario or name. Returns metadata only — does not inject manuals into context.",
            use_when="Looking for a reusable procedure or domain manual that may help the current task. Checking if a skill exists before creating one.",
            do_not_use_when="Recalling durable facts (use recall_memory). Getting routing advice (use advise_behavior). You already know the skill name and want its manual (use skill_read or skill_inject).",
            failure_next_steps="If no results, try broader scenario terms. If a skill exists but isn't matching, check its activation terms with skill_read.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderSearchInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderSearchOutput,
        execution=INDIRECT_LOCAL_READ,
        aliases=("skill_search",),
    )
    def search(self, call: CapabilityCall):
        return SkillSearchTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="read",
        guidance=ToolGuidance(
            purpose="Read one skill's metadata and optionally its full manual text.",
            use_when="Inspecting a specific skill's content, activation terms, or manual before deciding to inject it.",
            do_not_use_when="Searching for skills by scenario (use skill_search). Injecting a manual into active context (use skill_inject). Reading a durable fact (use recall_memory).",
            failure_next_steps="If the skill name is not found, use skill_search to discover it.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderReadInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderReadOutput,
        execution=INDIRECT_LOCAL_READ,
        aliases=("skill_read",),
    )
    def read(self, call: CapabilityCall):
        return SkillReadTool(service=self.service).invoke(_skill_name_args(call.args))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="inject",
        guidance=ToolGuidance(
            purpose="Inject a skill's manual text into the current context as a reference observation.",
            use_when="A skill matches the current task and you need its step-by-step procedure or domain manual to guide execution.",
            do_not_use_when="Just browsing skill metadata (use skill_read). Searching for skills (use skill_search). The skill is already injected (check active skills in system prompt).",
            failure_next_steps="If the skill name is not found or inactive, use skill_search to find an active one. Injected manuals are reference only — they do not override user instructions or policy.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderInjectInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderInjectOutput,
        execution=INDIRECT_LOCAL_READ,
        aliases=("skill_inject",),
    )
    def inject(self, call: CapabilityCall):
        return SkillInjectTool(service=self.service).invoke(_skill_name_args(call.args))


def _skill_name_args(raw: dict[str, object]) -> dict[str, object]:
    args = dict(raw)
    name = str(args.pop("name", "") or "").strip()
    if name:
        args["skill_id"] = name
    return args


def register_with_core(context: "MainContext", service: SkillService) -> ModuleHandle:
    from pal.skill.prompt import SkillPromptFragmentProvider

    service.execution_runtime = service.execution_runtime or context.execution_runtime
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
