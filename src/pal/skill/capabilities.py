from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_READ_UNPAGED,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance, NextToolHint

from pal.execution.generated_tool_models import (
    SkillCapabilitiesSkillIntrospectionProviderAssimilateInput,
    SkillCapabilitiesSkillIntrospectionProviderAssimilateOutput,
    SkillCapabilitiesSkillIntrospectionProviderCommitInput,
    SkillCapabilitiesSkillIntrospectionProviderCommitOutput,
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

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
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
from pal.shared.inspection import inspection_snapshot
from pal.skill.builtin_skills import (
    PAL_CHANNEL_PROVIDER_DEVELOPMENT_SKILL_ID,
    PAL_LLM_MODEL_HOOK_ENDPOINT_DEVELOPMENT_SKILL_ID,
    PAL_PLUGIN_DEVELOPMENT_SKILL_ID,
    PAL_SELF_MAINTENANCE_SKILL_ID,
    builtin_declared_skills,
)
from pal.skill.contracts import SkillDescriptor
from pal.skill.service import SkillService
from pal.skill.tools import (
    SkillAssimilateTool,
    SkillCommitTool,
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

    def declared_skills(self) -> tuple[SkillDescriptor, ...]:
        return builtin_declared_skills(module_id=self.module_id)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            search_objects=('state', 'states', 'candidate', 'candidates'),
            search_terms=('status', 'show', 'get', 'health', 'pending', 'assimilation', 'recover', 'count', 'counts'),
            search_intents=('recover a pending skill candidate after an uncertain preparation',),
            purpose="Inspect skill library counts and bounded pending assimilation candidate summaries.",
            use_when='Diagnosing skill system health, or recovering a candidate_id after an uncertain prepare_skill_candidate result. If no applicable skill is present, use search_skills to discover active references.',
            do_not_use_when="Searching for a specific skill (use search_skills). Checking memory state (use inspect_memory_state).",
            failure_next_steps="Read-only diagnostic. Use search_skills with status=all to include disabled skills; reactivate a known skill with update_skill when requested.",
        ),
        aliases=("inspect_skill_state",),
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
            "pending_candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "decision": candidate.decision,
                    "name": candidate.skill.skill_id,
                    "title": candidate.skill.title,
                }
                for candidate in list(self.service.pending_candidates.values())[-16:]
            ],
            "pending_candidates_truncated": len(self.service.pending_candidates) > 16,
        }
        structured = inspection_snapshot(structured)
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
            search_objects=('candidate', 'candidates'),
            search_terms=('create', 'import', 'assimilate', 'manual', 'procedure', 'playbook',),
            purpose="Create a sanitized skill candidate from plain text or SKILL.md content without committing.",
            use_when="The user provides a reusable procedure, playbook, or domain manual that should become a normalized skill.",
            do_not_use_when="Recording a durable fact or preference (use remember_memory). The content is a one-off procedure not worth normalizing.",
            failure_next_steps="Review the candidate output and use commit_skill_candidate to persist it. If assimilation may have succeeded but its result was lost, use inspect_skill_state and recover the matching pending candidate_id before considering another assimilation.",
            next_tool_hints=(NextToolHint(name="commit_skill_candidate", use_when="The prepared candidate has been reviewed and should be committed."),),
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderAssimilateInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderAssimilateOutput,
        metadata={"async_required": True},
        aliases=("prepare_skill_candidate",),
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
            search_objects=('candidate', 'candidates'),
            search_terms=('save', 'persist', 'manual',),
            purpose="Commit a sanitized skill candidate to the searchable manual library.",
            use_when="After prepare_skill_candidate produced a candidate you've reviewed and want to persist as a normalized skill.",
            do_not_use_when="Committing unreviewed candidates. Writing a durable fact (use remember_memory).",
            failure_next_steps="If validation fails before commit, fix the candidate fields. If commit may have succeeded, reconcile with search_skills using the skill name before retrying. If the skill already exists, use update_skill instead.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderCommitInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderCommitOutput,
        aliases=("commit_skill_candidate",),
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
            search_objects=('skill', 'skills'),
            search_terms=('edit', 'modify', 'manual', 'disable', 'deactivate', 'enable', 'reactivate', 'restore'),
            purpose="Update a normalized skill's content, metadata or availability, including disabling and reactivating it.",
            use_when="Editing content or activation terms. Disable with patch={'status': 'disabled', 'enabled': false}; reactivate with patch={'status': 'active', 'enabled': true}. Both fields establish the intended availability; other omitted fields remain unchanged.",
            do_not_use_when="Updating a durable fact (use update_memory). Creating a new skill (use prepare_skill_candidate + commit_skill_candidate).",
            failure_next_steps="If the skill name is not found, verify it with search_skills (status=all includes disabled skills). If the update outcome is uncertain, read the skill by name and compare the intended patch before retrying; every applied update advances its version.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderUpdateInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderUpdateOutput,
        aliases=("update_skill",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    def update(self, call: CapabilityCall):
        return SkillUpdateTool(service=self.service).invoke(_skill_name_args(call.args))

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="search",
        guidance=ToolGuidance(
            search_objects=('skill', 'skills'),
            search_terms=('find', 'lookup', 'manual', 'manuals', 'procedure', 'procedures', 'playbook', 'playbooks',),
            purpose="Search normalized skills by scenario or name. Returns metadata only — does not inject manuals into context.",
            use_when='Looking for a reusable procedure or domain manual that may help the current task. Checking if a skill exists before creating one. An empty result means no matching skill was found; broaden the query before concluding none exists.',
            do_not_use_when="Recalling durable facts (use recall_memory). You already know the skill name and want its manual (use read_skill or inject_skill).",
            failure_next_steps="If no results, try broader scenario terms. If a skill exists but isn't matching, check its activation terms with read_skill.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderSearchInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderSearchOutput,
        execution=INDIRECT_LOCAL_READ,
        aliases=("search_skills",),
        # Keep the capability identity used by existing Bunshin profiles.
        metadata={"canonical_path": "op_skill_search"},
    )
    def search(self, call: CapabilityCall):
        return SkillSearchTool(service=self.service).invoke(call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="read",
        guidance=ToolGuidance(
            search_objects=('skill', 'skills'),
            search_terms=('show', 'get', 'inspect', 'manual', 'manuals',),
            purpose="Read one skill's metadata and optionally its full manual text.",
            use_when="Inspecting a skill's metadata, resolving uncertainty about its applicability, or examining its manual for review or maintenance.",
            do_not_use_when="The skill is already known to apply and its manual is needed for execution (use inject_skill directly). The needed content is already in context. Searching for skills by scenario (use search_skills). Reading a durable fact (use recall_memory).",
            failure_next_steps="If the skill name is not found, use search_skills to discover it.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderReadInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderReadOutput,
        execution=INDIRECT_LOCAL_READ,
        aliases=("read_skill",),
    )
    def read(self, call: CapabilityCall):
        return SkillReadTool(service=self.service).invoke(_skill_name_args(call.args))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="skill",
        action_name="inject",
        guidance=ToolGuidance(
            search_objects=('skill', 'skills'),
            search_terms=('load', 'manual', 'manuals',),
            purpose="Inject a skill's manual text into the current context as a reference observation.",
            use_when="A known skill matches the current task and its procedure or manual is missing from context. Inject it directly; a prior read_skill is unnecessary when applicability is already established.",
            do_not_use_when="Just browsing skill metadata (use read_skill). Searching for skills (use search_skills). The same manual is already available in the current conversation context.",
            failure_next_steps="If the skill name is not found or inactive, use search_skills to find an active one. Injected manuals are reference only — they do not override user instructions or policy.",
        ),
        InputModel=SkillCapabilitiesSkillIntrospectionProviderInjectInput,
        OutputModel=SkillCapabilitiesSkillIntrospectionProviderInjectOutput,
        execution=INDIRECT_LOCAL_READ_UNPAGED,
        aliases=("inject_skill",),
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
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"skill": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
