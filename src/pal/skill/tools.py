from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.prompt_rendering import render_system_reminder
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.skill.service import SkillService


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
        payload["manual_text"] = "[omitted; call skill_inject or read with include_manual=true if needed]"
    return payload


@dataclass
class SkillAssimilateTool:
    service: SkillService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        structured = {"reason": "async_required", "tool": "skill_assimilate"}
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="skill_assimilate requires an active async turn context.",
            structured=structured,
            llm_text=_render_skill_tool_payload(self.service, "Skill assimilation unavailable", structured),
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
                llm_text=_render_skill_tool_payload(self.service, "Skill assimilation failed", structured),
            )
        structured = candidate.to_dict()
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill candidate created",
            structured=structured,
            llm_text=_render_skill_tool_payload(self.service, "Skill candidate", structured),
        )


@dataclass
class SkillCommitTool:
    service: SkillService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        try:
            structured = self.service.commit_candidate(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill commit failed",
                structured=structured,
                llm_text=_render_skill_tool_payload(self.service, "Skill commit failed", structured),
            )
        except Exception as exc:
            structured = {"reason": "commit_failed", "error": f"{exc.__class__.__name__}: {exc}"}
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="skill commit failed",
                structured=structured,
                llm_text=_render_skill_tool_payload(self.service, "Skill commit failed", structured),
            )
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill committed",
            structured=structured,
            llm_text=_render_skill_tool_payload(self.service, "Skill committed", structured),
        )


@dataclass
class SkillUpdateTool:
    service: SkillService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        try:
            skill = self.service.update_skill(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="skill update failed",
                structured=structured,
                llm_text=_render_skill_tool_payload(self.service, "Skill update failed", structured),
            )
        structured = {"skill": skill.to_dict()}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill updated",
            structured=structured,
            llm_text=_render_skill_tool_payload(self.service, "Skill updated", structured),
        )


@dataclass
class SkillDisableTool:
    service: SkillService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        skill_id = str(args.get("skill_id") or "").strip()
        skill = self.service.disable_skill(skill_id)
        if skill is None:
            structured = {"reason": "skill_not_found", "skill_id": skill_id}
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="skill not found",
                structured=structured,
                llm_text=_render_skill_tool_payload(self.service, "Skill disable failed", structured),
            )
        structured = {"skill": skill.to_dict()}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill disabled",
            structured=structured,
            llm_text=_render_skill_tool_payload(self.service, "Skill disabled", structured),
        )


@dataclass
class SkillSearchTool:
    service: SkillService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        query = str(args.get("query") or "").strip().lower()
        if not query:
            structured = {"reason": "invalid_request", "field": "query"}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="query is required",
                structured=structured,
                llm_text=_render_skill_tool_payload(self.service, "Skill search failed", structured),
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
                "name": skill.skill_id,
                "score": round(score, 4),
                "match_reason": reason,
                "manual_chars": len(skill.manual_text),
                "injectable": bool(skill.active),
            }
            if avoid_overlap:
                hit["avoid_when_overlap"] = True
            ranked.append(hit)
        ranked.sort(key=lambda item: (-float(item["score"]), str(item["skill_id"])))
        hits = ranked[:top_k]
        structured = {"hits": hits, "count": len(hits)}
        has_injectable_hit = any(bool(hit.get("injectable")) for hit in hits)
        if has_injectable_hit:
            structured["next_action"] = "To use a matched active skill, call skill_inject with its name before answering from it."
        llm_text = _render_skill_tool_payload(self.service, "Skill search", structured)
        if has_injectable_hit:
            llm_text = (
                "Skill search found an injectable active skill. "
                "If the user asked to use this skill, the next tool call MUST be skill_inject with the matched name. "
                "Search alone is not using the skill.\n"
                f"{llm_text}"
            )
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"found {len(hits)} skill(s)",
            structured=structured,
            llm_text=llm_text,
        )


@dataclass
class SkillReadTool:
    service: SkillService

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
                llm_text=_render_skill_tool_payload(self.service, "Skill read failed", structured),
            )
        structured = {"skill": skill_read_dict(skill, include_manual=include_manual)}
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="skill read",
            structured=structured,
            llm_text=_render_skill_tool_payload(self.service, "Skill read", structured),
        )


@dataclass
class SkillInjectTool:
    service: SkillService

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
                llm_text=_render_skill_tool_payload(self.service, "Skill injection failed", structured),
            )
        skill = self.service.inject_skill(skill_id)
        if skill is None:
            structured = {"reason": "skill_not_found_or_inactive", "skill_id": skill_id}
            return CapabilityResult(
                status=RuntimeStatus.NOT_FOUND,
                text="skill not found or inactive",
                structured=structured,
                llm_text=_render_skill_tool_payload(self.service, "Skill injection failed", structured),
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
            llm_text=render_system_reminder(_project_skill_text(self.service, _render_injected_skill_for_llm(structured))),
        )


def _render_injected_skill_for_llm(payload: dict[str, Any]) -> str:
    lines = ["Injected skill:"]
    title = str(payload.get("title") or "").strip()
    skill_id = str(payload.get("skill_id") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    use_when = str(payload.get("use_when") or "").strip()
    avoid_when = str(payload.get("avoid_when") or "").strip()
    manual = str(payload.get("manual_text") or "").strip()
    capability_refs = [str(item).strip() for item in list(payload.get("capability_refs") or []) if str(item).strip()]
    if title:
        lines.append(f"Title: {title}")
    if skill_id:
        lines.append(f"Skill id: {skill_id}")
    if summary:
        lines.append(f"Summary: {summary}")
    if use_when:
        lines.append(f"Use when: {use_when}")
    if avoid_when:
        lines.append(f"Avoid when: {avoid_when}")
    if capability_refs:
        lines.append(f"Capability refs: {', '.join(capability_refs)}")
    if manual:
        lines.extend(["", "Manual:", manual])
    return "\n".join(lines).strip()


def _render_skill_tool_payload(service: SkillService, title: str, structured: Any) -> str:
    runtime = service.execution_runtime
    projector = getattr(runtime, "project_llm_value", None)
    llm_value = projector(structured) if callable(projector) else structured
    return render_titled_structured_for_llm(title, llm_value)


def _project_skill_text(service: SkillService, value: object) -> str:
    runtime = service.execution_runtime
    projector = getattr(runtime, "project_llm_text", None)
    return str(projector(value)) if callable(projector) else str(value or "")


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
