from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.behavior.contracts import AFFORDANCE_VISIBILITY_DISCOVERABLE, AFFORDANCE_VISIBILITY_RESIDENT, BehaviorAdviceRequest
from pal.behavior.service import BehaviorLearnConflict, BehaviorService
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


BEHAVIOR_ADVICE_DESCRIPTION = (
    "Ask Pal's behavior router which capabilities, skills, memory query hints, or route guidance may fit the current "
    "scenario. Behavior is Pal's condition-reflex layer: when situation X appears, consider route/action Y. "
    "Use when the task route is ambiguous, risky, multi-step, unfamiliar, design/debug/recovery oriented, "
    "or the next capability is unclear. Skip when current context is sufficient, the user gave a clear direct "
    "implementation command, a single visible capability obviously matches, or the failure is an obvious local/schema/input mistake. "
    "Treat the result as routing resources, not orders."
)

BEHAVIOR_LEARN_DESCRIPTION = (
    "Learn a future behavior rule in Pal's condition-reflex layer: when situation X appears, Pal should consider route/action Y. "
    "Use only when the user explicitly asks Pal to learn/adopt/follow a future behavior rule or clearly teaches a durable route preference. "
    "Do not use for durable facts, ordinary preferences, runtime state, reusable procedures, or memory cases; use remember_memory for facts to remember. "
    "If the same scenario already has behavior guidance, the default conflict_resolution='ask' returns a structured user-decision request instead of overwriting."
)

BEHAVIOR_UPDATE_DESCRIPTION = (
    "Update persisted database behavior guidance by matching the original behavior text. "
    "For replacing the visible guidance line shown to Pal, set prompt_hint to the new text. "
    "Pass the original rendered guidance line as affordance, not an internal id. "
    "scenario_text is only the activation scenario. Injected/plugin guidance is read-only here. "
    "Do not claim behavior guidance changed unless this tool confirms success."
)

BEHAVIOR_FORGET_DESCRIPTION = (
    "Forget persisted database behavior guidance by matching the original behavior text. Pass the original rendered "
    "guidance line as affordance, not an internal id. Injected/plugin guidance is read-only here. Do not claim "
    "behavior guidance changed unless this tool confirms success."
)

@dataclass
class BehaviorAdviceTool:
    service: BehaviorService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        structured = {"reason": "async_required", "tool": "advise_behavior"}
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="behavior_advise requires an active async turn context.",
            structured=structured,
            llm_text=_render_behavior_tool_payload(self.service, "Behavior advice unavailable", structured),
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        request = _advice_request_from_args(args)
        result = await self.service.advise_async(request)
        structured = result.to_dict()
        llm_payload = _advice_llm_payload(structured)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"behavior advice returned {len(result.candidates)} candidate(s)",
            structured=structured,
            llm_text=_render_behavior_tool_payload(self.service, "Behavior advice", llm_payload),
        )


@dataclass
class AffordanceSubmitTool:
    service: BehaviorService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        return self._submit(args)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self._submit(args)

    def _submit(self, args: dict[str, Any]) -> CapabilityResult:
        payload = _normalize_affordance_mutation_args(args)
        try:
            descriptor = self.service.submit_affordance(payload)
        except BehaviorLearnConflict as exc:
            structured = exc.to_payload()
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="behavior learning needs user decision",
                structured=structured,
                llm_text=_render_behavior_tool_payload(self.service, "Behavior learning needs user decision", structured),
            )
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="behavior learning failed",
                structured=structured,
                llm_text=_render_behavior_tool_payload(self.service, "Behavior learning failed", structured),
            )
        learn_result = str((descriptor.metadata or {}).get("_learn_behavior_result") or "learned")
        structured = {
            "affordance_id": descriptor.affordance_id,
            "module_id": descriptor.module_id,
            "title": descriptor.title,
            "learn_result": learn_result,
            "source_kind": descriptor.source_kind,
            "scenario_text": descriptor.scenario_text,
            "prompt_hint": descriptor.prompt_hint,
            "capability_refs": list(descriptor.capability_refs),
            "skill_refs": list(descriptor.skill_refs),
            "memory_query_hints": list(descriptor.memory_query_hints),
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="behavior guidance unchanged" if learn_result == "skipped" else "behavior guidance learned",
            structured=structured,
            llm_text=_render_behavior_tool_payload(
                self.service,
                "Behavior guidance unchanged" if learn_result == "skipped" else "Behavior guidance learned",
                structured,
            ),
        )


@dataclass
class AffordanceUpdateTool:
    service: BehaviorService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        return self._update(args)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self._update(args)

    def _update(self, args: dict[str, Any]) -> CapabilityResult:
        payload = _normalize_affordance_mutation_args(args)
        try:
            descriptor = self.service.update_affordance(payload)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="behavior guidance update failed",
                structured=structured,
                llm_text=_render_behavior_tool_payload(self.service, "Behavior guidance update failed", structured),
            )
        updated_fields = [k for k in payload if k not in {"affordance", "affordance_id"} and payload[k] is not None]
        structured = {
            "affordance_id": descriptor.affordance_id,
            "affordance_hash": self.service.affordance_text_hash(descriptor),
            "module_id": descriptor.module_id,
            "title": descriptor.title,
            "scenario_text": descriptor.scenario_text,
            "prompt_hint": descriptor.prompt_hint,
            "updated_fields": updated_fields,
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="behavior guidance updated",
            structured=structured,
            llm_text=_render_behavior_tool_payload(self.service, "Behavior guidance updated", structured),
        )


@dataclass
class AffordanceDeleteTool:
    service: BehaviorService

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        return self._delete(args)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self._delete(args)

    def _delete(self, args: dict[str, Any]) -> CapabilityResult:
        try:
            descriptor = self.service.delete_affordance(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="behavior forgetting failed",
                structured=structured,
                llm_text=_render_behavior_tool_payload(self.service, "Behavior forgetting failed", structured),
            )
        structured = {
            "affordance_id": descriptor.affordance_id,
            "affordance_hash": self.service.affordance_text_hash(descriptor),
            "module_id": descriptor.module_id,
            "title": descriptor.title,
            "deleted": True,
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="behavior guidance forgotten",
            structured=structured,
            llm_text=_render_behavior_tool_payload(self.service, "Behavior guidance forgotten", structured),
        )


def _advice_request_from_args(args: dict[str, Any]) -> BehaviorAdviceRequest:
    return BehaviorAdviceRequest(
        scenario=str(args.get("scenario") or ""),
        intent=str(args.get("intent") or ""),
        turn_kind=str(args.get("turn_kind") or "chat"),
        constraints=tuple(str(item) for item in (args.get("constraints") or ()) if str(item).strip()),
        already_considered=tuple(str(item) for item in (args.get("already_considered") or ()) if str(item).strip()),
        top_k=int(args.get("top_k") or 5),
    )


def _normalize_affordance_mutation_args(args: dict[str, Any]) -> dict[str, Any]:
    payload = dict(args or {})
    if "resident" in payload and "visibility_mode" not in payload:
        payload["visibility_mode"] = AFFORDANCE_VISIBILITY_RESIDENT if bool(payload.get("resident")) else AFFORDANCE_VISIBILITY_DISCOVERABLE
    payload.pop("resident", None)
    return payload


def _advice_llm_payload(structured: dict[str, Any]) -> dict[str, Any]:
    raw_candidates = structured.get("candidates")
    safe_candidates: list[dict[str, Any]] = []
    if isinstance(raw_candidates, list):
        for index, raw_candidate in enumerate(raw_candidates, start=1):
            if not isinstance(raw_candidate, dict):
                continue
            title = str(raw_candidate.get("title") or "").strip() or f"Route {index}"
            item: dict[str, Any] = {"title": title}
            hint = str(raw_candidate.get("prompt_hint") or "").strip()
            if hint:
                item["prompt_hint"] = hint
            capability_refs = _string_list(raw_candidate.get("capability_refs"))
            if capability_refs:
                item["capability_refs"] = capability_refs
            skill_refs = _string_list(raw_candidate.get("skill_refs"))
            if skill_refs:
                item["skill_refs"] = skill_refs
            memory_query_hints = _string_list(raw_candidate.get("memory_query_hints"))
            if memory_query_hints:
                item["memory_query_hints"] = memory_query_hints
            safe_candidates.append(item)
    payload: dict[str, Any] = {"candidates": safe_candidates}
    if bool(structured.get("fallback_used")):
        payload["fallback_used"] = True
    if str(structured.get("router_error") or "").strip():
        payload["router_error"] = "present"
    return payload


def _render_behavior_tool_payload(service: BehaviorService, title: str, structured: Any) -> str:
    runtime = service.execution_runtime
    projector = getattr(runtime, "project_llm_value", None)
    llm_value = projector(structured) if callable(projector) else structured
    return render_titled_structured_for_llm(title, llm_value)


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
