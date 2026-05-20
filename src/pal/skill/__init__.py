from pal.skill.contracts import (
    SKILL_ADMISSION_MANUAL_CHAR_BUDGET,
    SKILL_INJECT_MANUAL_CHAR_BUDGET,
    SKILL_SOURCE_DECLARED,
    SKILL_SOURCE_INSTRUCTED,
    SKILL_SOURCE_LEARNED,
    SKILL_STATUS_ACTIVE,
    SKILL_STATUS_DEPRECATED,
    SKILL_STATUS_DISABLED,
    SKILL_STATUS_DRAFT,
    SKILL_STATUS_NEEDS_REVIEW,
    SkillApplicabilitySTAR,
    SkillAssimilationCandidate,
    SkillDescriptor,
    SkillInjectRequest,
)

__all__ = [
    "SKILL_INJECT_MANUAL_CHAR_BUDGET",
    "SKILL_ADMISSION_MANUAL_CHAR_BUDGET",
    "SKILL_SOURCE_DECLARED",
    "SKILL_SOURCE_INSTRUCTED",
    "SKILL_SOURCE_LEARNED",
    "SKILL_STATUS_ACTIVE",
    "SKILL_STATUS_DEPRECATED",
    "SKILL_STATUS_DISABLED",
    "SKILL_STATUS_DRAFT",
    "SKILL_STATUS_NEEDS_REVIEW",
    "SkillApplicabilitySTAR",
    "SkillAssimilationCandidate",
    "SkillDescriptor",
    "SkillInjectRequest",
    "SkillAssimilateTool",
    "SkillCommitTool",
    "SkillDisableTool",
    "SkillInjectTool",
    "SkillIntrospectionProvider",
    "SkillModel",
    "SkillReadTool",
    "SkillRepository",
    "SkillSearchTool",
    "SkillService",
    "SkillUpdateTool",
    "register_with_core",
]


def __getattr__(name: str):
    if name == "SkillModel":
        from pal.skill.models import SkillModel

        return SkillModel
    if name == "SkillRepository":
        from pal.skill.repository import SkillRepository

        return SkillRepository
    if name == "SkillService":
        from pal.skill.service import SkillService

        return SkillService
    if name in {
        "SkillAssimilateTool",
        "SkillCommitTool",
        "SkillDisableTool",
        "SkillInjectTool",
        "SkillReadTool",
        "SkillSearchTool",
        "SkillUpdateTool",
    }:
        from pal.skill import tools

        return getattr(tools, name)
    if name in {"SkillIntrospectionProvider", "register_with_core"}:
        from pal.skill.introspection import SkillIntrospectionProvider, register_with_core

        return {"SkillIntrospectionProvider": SkillIntrospectionProvider, "register_with_core": register_with_core}[name]
    raise AttributeError(name)
