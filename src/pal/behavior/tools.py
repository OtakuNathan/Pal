from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.behavior.contracts import AFFORDANCE_VISIBILITY_DISCOVERABLE, AFFORDANCE_VISIBILITY_RESIDENT, BehaviorAdviceRequest
from pal.behavior.service import BehaviorLearnConflict, BehaviorService
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus, replace_internal_tool_names_in_value
from pal.shared.result_rendering import render_titled_structured_for_llm


ADVISE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "description": "Current situation Pal is facing; include the routing uncertainty or risky decision point."},
        "intent": {"type": "string", "description": "Optional intended outcome."},
        "turn_kind": {"type": "string", "description": "Turn type, such as chat, service, or minion."},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "already_considered": {"type": "array", "items": {"type": "string"}},
        "top_k": {"type": "integer", "minimum": 0, "default": 5},
    },
    "required": ["scenario"],
}

ADVISE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {"type": "array", "items": {"type": "object"}},
        "fallback_used": {"type": "boolean"},
        "router_error": {"type": "string"},
    },
}

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

AFFORDANCE_SUBMIT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario_text": {"type": "string", "description": "Scenario that should activate this affordance."},
        "prompt_hint": {
            "type": "string",
            "description": "Short behavioral hint body Pal should remember. Do not repeat the title as a prefix.",
        },
        "title": {"type": "string", "description": "Optional short label for this behavior guidance."},
        "activation_terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional concrete terms that help match this scenario later.",
        },
        "capability_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional exact tool/capability names this behavior may route toward.",
        },
        "skill_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional skill ids that may provide reference manuals for this scenario.",
        },
        "memory_query_hints": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional recall_memory query hints for facts/cases relevant to this behavior.",
        },
        "conflict_resolution": {
            "type": "string",
            "enum": ["ask", "merge", "overwrite", "skip"],
            "default": "ask",
            "description": (
                "What to do when the same scenario already has behavior guidance. "
                "Use ask by default so Pal asks the user whether to merge, overwrite, or leave it unchanged."
            ),
        },
        "resident": {
            "type": "boolean",
            "default": False,
            "description": (
                "Set true only for behavior guidance that should be always visible in Pal's prompt. "
                "Leave false for normal guidance that the behavior router recalls when the scenario matches."
            ),
        },
    },
    "required": ["scenario_text", "prompt_hint"],
}

AFFORDANCE_SUBMIT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "affordance_id": {"type": "string"},
        "learn_result": {"type": "string", "enum": ["learned", "merged", "overwritten", "skipped"]},
        "source_kind": {"type": "string"},
        "scenario_text": {"type": "string"},
        "prompt_hint": {"type": "string"},
        "reason": {"type": "string"},
        "action_required": {"type": "string"},
        "conflict_resolution_options": {"type": "array", "items": {"type": "string"}},
        "candidates": {"type": "array", "items": {"type": "object"}},
    },
}


@dataclass
class BehaviorAdviceTool:
    service: BehaviorService
    name: str = "op_behavior_advise"
    display_name: str = "Behavior advice"
    family: str = "behavior"
    description: str = BEHAVIOR_ADVICE_DESCRIPTION
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("behavior", "affordance", "routing")
    keywords: tuple[str, ...] = ("affordance", "skill", "capability", "scenario")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = ADVISE_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = ADVISE_RESULT_SCHEMA

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        structured = {"reason": "async_required", "tool": self.name}
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="behavior_advise requires an active async turn context.",
            structured=structured,
            llm_text=_render_behavior_tool_payload("Behavior advice unavailable", structured),
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
            llm_text=_render_behavior_tool_payload("Behavior advice", llm_payload),
        )


@dataclass
class AffordanceSubmitTool:
    service: BehaviorService
    name: str = "op_behavior_save"
    display_name: str = "Learn behavior"
    family: str = "behavior"
    description: str = BEHAVIOR_LEARN_DESCRIPTION
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("behavior", "affordance", "learn")
    keywords: tuple[str, ...] = ("affordance", "instructed", "learned", "behavior", "remember")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = AFFORDANCE_SUBMIT_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = AFFORDANCE_SUBMIT_RESULT_SCHEMA

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
                llm_text=_render_behavior_tool_payload("Behavior learning needs user decision", structured),
            )
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="behavior learning failed",
                structured=structured,
                llm_text=_render_behavior_tool_payload("Behavior learning failed", structured),
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
                "Behavior guidance unchanged" if learn_result == "skipped" else "Behavior guidance learned",
                structured,
            ),
        )


AFFORDANCE_UPDATE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "affordance": {
            "type": "string",
            "description": "Original behavior guidance text to match. Pass the affordance text itself; Pal resolves the internal record.",
        },
        "affordance_id": {"type": "string", "description": "Legacy exact affordance_id. Prefer affordance text instead."},
        "scenario_text": {
            "type": "string",
            "description": (
                "Updated activation scenario text. Do not use this when replacing the visible behavior guidance "
                "shown in <behavior_guidance>; use prompt_hint for that."
            ),
        },
        "prompt_hint": {
            "type": "string",
            "description": (
                "Updated visible behavior guidance body rendered in <behavior_guidance>. Use this when the user "
                "asks to replace, edit, or update the guidance/original text. Do not repeat the title as a prefix."
            ),
        },
        "title": {"type": "string"},
        "activation_terms": {"type": "array", "items": {"type": "string"}},
        "capability_refs": {"type": "array", "items": {"type": "string"}},
        "skill_refs": {"type": "array", "items": {"type": "string"}},
        "memory_query_hints": {"type": "array", "items": {"type": "string"}},
        "resident": {
            "type": "boolean",
            "description": (
                "Set true to make this guidance always visible in Pal's prompt, or false to keep it behavior-router recalled."
            ),
        },
    },
    "required": ["affordance"],
}

AFFORDANCE_UPDATE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "affordance_id": {"type": "string"},
        "affordance_hash": {"type": "string"},
        "updated_fields": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class AffordanceUpdateTool:
    service: BehaviorService
    name: str = "op_behavior_affordance_update"
    display_name: str = "Update behavior"
    family: str = "behavior"
    description: str = BEHAVIOR_UPDATE_DESCRIPTION
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("behavior", "affordance", "update")
    keywords: tuple[str, ...] = ("affordance", "update", "behavior", "edit")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = AFFORDANCE_UPDATE_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = AFFORDANCE_UPDATE_RESULT_SCHEMA

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
                llm_text=_render_behavior_tool_payload("Behavior guidance update failed", structured),
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
            llm_text=_render_behavior_tool_payload("Behavior guidance updated", structured),
        )


AFFORDANCE_DELETE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "affordance": {
            "type": "string",
            "description": "Original behavior guidance text to match. Pass the affordance text itself; Pal resolves the internal record.",
        },
        "affordance_id": {"type": "string", "description": "Legacy exact affordance_id. Prefer affordance text instead."},
    },
    "required": ["affordance"],
}

AFFORDANCE_DELETE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "affordance_id": {"type": "string"},
        "affordance_hash": {"type": "string"},
        "deleted": {"type": "boolean"},
    },
}


@dataclass
class AffordanceDeleteTool:
    service: BehaviorService
    name: str = "op_behavior_affordance_delete"
    display_name: str = "Forget behavior"
    family: str = "behavior"
    description: str = BEHAVIOR_FORGET_DESCRIPTION
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("behavior", "affordance", "forget")
    keywords: tuple[str, ...] = ("affordance", "delete", "behavior", "forget")

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = AFFORDANCE_DELETE_ARGS_SCHEMA
        if self.result_schema is None:
            self.result_schema = AFFORDANCE_DELETE_RESULT_SCHEMA

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
                llm_text=_render_behavior_tool_payload("Behavior forgetting failed", structured),
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
            llm_text=_render_behavior_tool_payload("Behavior guidance forgotten", structured),
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


def _render_behavior_tool_payload(title: str, structured: Any) -> str:
    return render_titled_structured_for_llm(title, replace_internal_tool_names_in_value(structured))


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
