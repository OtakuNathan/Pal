from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.skill.contracts import SKILL_INJECT_MANUAL_CHAR_BUDGET
from pal.skill.service import SkillService


SKILL_ASSIMILATE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "source_text": {"type": "string", "description": "Text or SKILL.md content to turn into a Pal skill candidate."},
        "source_format": {"type": "string", "enum": ["plain_text", "skill_md"], "default": "plain_text"},
        "intent": {"type": "string", "enum": ["learn", "summarize", "sanitize"], "default": "learn"},
        "desired_skill_id": {"type": "string"},
    },
    "required": ["source_text"],
}

SKILL_COMMIT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": {"type": "string"},
        "candidate": {"type": "object"},
        "replace": {"type": "boolean", "default": False},
    },
}

SKILL_UPDATE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "patch": {"type": "object"},
    },
    "required": ["skill_id", "patch"],
}

SKILL_DISABLE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {"skill_id": {"type": "string"}},
    "required": ["skill_id"],
}

SKILL_INJECT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {"skill_id": {"type": "string", "description": "Skill id to inject."}},
    "required": ["skill_id"],
}

SKILL_INJECT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "use_when": {"type": "string"},
        "avoid_when": {"type": "string"},
        "applicability_star": {"type": "object"},
        "manual_text": {"type": "string"},
        "capability_refs": {"type": "array", "items": {"type": "string"}},
    },
}

SKILL_SEARCH_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Current scenario, user request, or explicit skill name to match."},
        "status": {"type": "string", "default": "active"},
        "top_k": {"type": "integer", "minimum": 1, "default": 5},
    },
    "required": ["query"],
}

SKILL_READ_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "include_manual": {"type": "boolean", "default": False},
    },
    "required": ["skill_id"],
}


def skill_summary_dict(skill) -> dict[str, Any]:
    return {
        "skill_id": skill.skill_id,
        "title": skill.title,
        "summary": skill.summary,
        "status": skill.status,
        "use_when": skill.use_when,
        "avoid_when": skill.avoid_when,
        "activation_terms": list(skill.activation_terms),
        "capability_refs": list(skill.capability_refs),
        "version": skill.version,
        "updated_at": skill.updated_at,
    }


def skill_read_dict(skill, *, include_manual: bool = False) -> dict[str, Any]:
    payload = skill_summary_dict(skill)
    payload.update(
        {
            "applicability_star": skill.applicability_star.to_dict(),
            "sanitization_notes": list(skill.sanitization_notes),
            "source_format": skill.source_format,
            "source_refs": list(skill.source_refs),
            "metadata": dict(skill.metadata),
        }
    )
    if include_manual:
        payload["manual_text"] = skill.manual_text
    else:
        payload["manual_chars"] = len(skill.manual_text)
        payload["manual_text"] = "[omitted; call op_skill_inject or read with include_manual=true if needed]"
    return payload


@dataclass
class SkillAssimilateTool:
    service: SkillService
    name: str = "op_skill_assimilate"
    display_name: str = "Assimilate skill"
    family: str = "skill"
    description: str = "Create a sanitized Pal skill candidate from plain text or SKILL.md content. Async only; does not commit."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill", "learn", "sanitize")
    keywords: tuple[str, ...] = ("skill", "learn", "summarize", "sanitize")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_ASSIMILATE_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = {"type": "object"}

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        structured = {"reason": "async_required", "tool": self.name}
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="op_skill_assimilate requires async execution.",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill assimilation unavailable", structured),
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        try:
            candidate = await self.service.assimilate_async(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill assimilation failed",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill assimilation failed", structured),
            )
        structured = candidate.to_dict()
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill candidate created",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill candidate", structured),
        )


@dataclass
class SkillCommitTool:
    service: SkillService
    name: str = "op_skill_commit"
    display_name: str = "Commit skill"
    family: str = "skill"
    description: str = "Commit a sanitized skill candidate and its thin affordance."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill", "write")
    keywords: tuple[str, ...] = ("skill", "commit", "save")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_COMMIT_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = {"type": "object"}

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        try:
            structured = self.service.commit_candidate(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill commit failed",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill commit failed", structured),
            )
        except Exception as exc:
            structured = {"reason": "commit_failed", "error": f"{exc.__class__.__name__}: {exc}"}
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="skill commit failed",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill commit failed", structured),
            )
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill committed",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill committed", structured),
        )


@dataclass
class SkillUpdateTool:
    service: SkillService
    name: str = "op_skill_update"
    display_name: str = "Update skill"
    family: str = "skill"
    description: str = "Update a normalized skill and refresh its thin affordance."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill", "write")
    keywords: tuple[str, ...] = ("skill", "update", "edit")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_UPDATE_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = {"type": "object"}

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        try:
            skill = self.service.update_skill(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill update failed",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill update failed", structured),
            )
        structured = {"skill": skill.to_dict()}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill updated",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill updated", structured),
        )


@dataclass
class SkillDisableTool:
    service: SkillService
    name: str = "op_skill_disable"
    display_name: str = "Disable skill"
    family: str = "skill"
    description: str = "Disable a normalized skill without deleting its history."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill", "write")
    keywords: tuple[str, ...] = ("skill", "disable")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_DISABLE_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = {"type": "object"}

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        skill_id = str(args.get("skill_id") or "").strip()
        skill = self.service.disable_skill(skill_id)
        if skill is None:
            structured = {"reason": "skill_not_found", "skill_id": skill_id}
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="skill not found",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill disable failed", structured),
            )
        structured = {"skill": skill.to_dict()}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill disabled",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill disabled", structured),
        )


@dataclass
class SkillSearchTool:
    service: SkillService
    name: str = "op_skill_search"
    display_name: str = "Search skills"
    family: str = "skill"
    description: str = "Search normalized Pal skills for the current scenario or explicit skill name. Does not return manuals."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill", "search")
    keywords: tuple[str, ...] = ("skill", "search", "find", "match")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_SEARCH_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = {"type": "object"}

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        query = str(args.get("query") or "").strip().lower()
        if not query:
            structured = {"reason": "invalid_request", "field": "query"}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="query is required",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill search failed", structured),
            )
        status_filter = str(args.get("status") or "active").strip()
        top_k = max(1, int(args.get("top_k") or 5))
        skills = self.service.repository.list_skills()
        if status_filter and status_filter != "all":
            skills = tuple(skill for skill in skills if skill.status == status_filter)
        ranked = []
        for skill in skills:
            score, reason, avoid_overlap = _skill_search_score(skill, query)
            if score <= 0:
                continue
            hit = {
                **skill_summary_dict(skill),
                "score": round(score, 4),
                "match_reason": reason,
                "manual_chars": len(skill.manual_text),
                "injectable": bool(skill.active and len(skill.manual_text) <= self.service.inject_manual_char_budget),
            }
            if avoid_overlap:
                hit["avoid_when_overlap"] = True
            ranked.append(hit)
        ranked.sort(key=lambda item: (-float(item["score"]), str(item["skill_id"])))
        hits = ranked[:top_k]
        structured = {"hits": hits, "count": len(hits)}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"found {len(hits)} skill(s)",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill search", structured),
        )


@dataclass
class SkillReadTool:
    service: SkillService
    name: str = "op_skill_read"
    display_name: str = "Read skill"
    family: str = "skill"
    description: str = "Read normalized Pal skill metadata, optionally including manual text."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill", "read")
    keywords: tuple[str, ...] = ("skill", "read", "inspect")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_READ_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = {"type": "object"}

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        skill_id = str(args.get("skill_id") or "").strip()
        include_manual = bool(args.get("include_manual", False))
        skill = self.service.repository.get_skill(skill_id)
        if skill is None:
            structured = {"reason": "skill_not_found", "skill_id": skill_id}
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="skill not found",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill read failed", structured),
            )
        structured = {"skill": skill_read_dict(skill, include_manual=include_manual)}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill read",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill read", structured),
        )


@dataclass
class SkillInjectTool:
    service: SkillService
    name: str = "op_skill_inject"
    display_name: str = "Skill injection"
    family: str = "skill"
    description: str = "Inject a registered active skill manual into the current tool observation."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("skill",)
    keywords: tuple[str, ...] = ("skill", "manual", "procedure")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = SKILL_INJECT_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = SKILL_INJECT_RESULT_SCHEMA

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        return self._inject(args)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self._inject(args)

    def _inject(self, args: dict[str, Any]) -> CapabilityResult:
        skill_id = str(args.get("skill_id") or "").strip()
        if not skill_id:
            structured = {"reason": "invalid_request", "field": "skill_id"}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill_id is required",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill injection failed", structured),
            )
        skill = self.service.inject_skill(skill_id)
        if skill is None:
            structured = {"reason": "skill_not_found_or_inactive", "skill_id": skill_id}
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="skill not found or inactive",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill injection failed", structured),
            )
        if len(skill.manual_text) > self.service.inject_manual_char_budget:
            structured = {
                "reason": "manual_too_long",
                "skill_id": skill_id,
                "manual_chars": len(skill.manual_text),
                "budget_chars": self.service.inject_manual_char_budget,
            }
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill manual is too long to inject safely",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Skill injection failed", structured),
            )
        structured = {
            "skill_id": skill.skill_id,
            "title": skill.title,
            "summary": skill.summary,
            "status": skill.status,
            "use_when": skill.use_when,
            "avoid_when": skill.avoid_when,
            "applicability_star": skill.applicability_star.to_dict(),
            "manual_text": skill.manual_text,
            "capability_refs": list(skill.capability_refs),
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=skill.manual_text,
            structured=structured,
            llm_text=render_titled_structured_for_llm("Skill manual", structured),
        )


def _skill_search_score(skill, query: str) -> tuple[float, str, bool]:
    terms = [term for term in str(query or "").lower().split() if term]
    if not terms:
        return 0.0, "", False
    strong_fields = " ".join((skill.skill_id, skill.title, " ".join(skill.activation_terms))).lower()
    star = skill.applicability_star
    normal_fields = " ".join(
        (
            skill.summary,
            skill.use_when,
            star.situation,
            star.task,
            star.action,
            star.result,
            " ".join(skill.capability_refs),
        )
    ).lower()
    avoid_fields = str(skill.avoid_when or "").lower()
    strong_hits = sum(1 for term in terms if term in strong_fields)
    normal_hits = sum(1 for term in terms if term in normal_fields)
    avoid_hits = sum(1 for term in terms if term in avoid_fields)
    if strong_hits == 0 and normal_hits == 0:
        return 0.0, "", bool(avoid_hits)
    raw = strong_hits * 3.0 + normal_hits * 1.0 - avoid_hits * 0.75
    score = max(0.01, raw / max(1, len(terms) * 3))
    reasons = []
    if strong_hits:
        reasons.append(f"{strong_hits} strong field match(es)")
    if normal_hits:
        reasons.append(f"{normal_hits} applicability/summary match(es)")
    if avoid_hits:
        reasons.append(f"{avoid_hits} avoid_when overlap(s)")
    return score, "; ".join(reasons), bool(avoid_hits)
