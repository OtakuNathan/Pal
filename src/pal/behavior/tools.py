from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.behavior.contracts import BehaviorAdviceRequest
from pal.behavior.service import BehaviorService
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


ADVISE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "description": "Current situation Pal is facing."},
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

AFFORDANCE_SUBMIT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario_text": {"type": "string", "description": "Scenario that should activate this affordance."},
        "prompt_hint": {"type": "string", "description": "Short behavioral hint Pal should remember."},
        "title": {"type": "string"},
        "activation_terms": {"type": "array", "items": {"type": "string"}},
        "capability_refs": {"type": "array", "items": {"type": "string"}},
        "skill_refs": {"type": "array", "items": {"type": "string"}},
        "memory_query_hints": {"type": "array", "items": {"type": "string"}},
        "visibility_mode": {"type": "string", "enum": ["resident", "discoverable"], "default": "discoverable"},
        "activation_kind": {"type": "string", "enum": ["deliberative", "reactive"], "default": "deliberative"},
        "activation_mode": {"type": "string", "enum": ["suggest", "automatic", "require_approval"], "default": "suggest"},
        "source_kind": {"type": "string", "enum": ["instructed", "learned"], "default": "instructed"},
        "priority": {"type": "integer", "default": 100},
        "activation_threshold": {"type": "number", "default": 0.25},
        "enabled": {"type": "boolean", "default": True},
    },
    "required": ["scenario_text", "prompt_hint"],
}

AFFORDANCE_SUBMIT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "affordance_id": {"type": "string"},
        "source_kind": {"type": "string"},
        "scenario_text": {"type": "string"},
        "prompt_hint": {"type": "string"},
    },
}


@dataclass
class BehaviorAdviceTool:
    service: BehaviorService
    name: str = "op_behavior_advise"
    display_name: str = "Behavior advice"
    family: str = "behavior"
    description: str = "Ask Pal's behavior router which capabilities or skills may fit the current scenario. Async only."
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
            text="op_behavior_advise requires async execution.",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Behavior advice unavailable", structured),
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        request = _advice_request_from_args(args)
        result = await self.service.advise_async(request)
        structured = result.to_dict()
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"behavior advice returned {len(result.candidates)} candidate(s)",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Behavior advice", structured),
        )


@dataclass
class AffordanceSubmitTool:
    service: BehaviorService
    name: str = "op_behavior_affordance_submit"
    display_name: str = "Submit affordance"
    family: str = "behavior"
    description: str = "Persist a user-instructed or learned affordance. This is not for ordinary memory cases."
    args_schema: dict[str, Any] = None  # type: ignore[assignment]
    result_schema: dict[str, Any] = None  # type: ignore[assignment]
    tags: tuple[str, ...] = ("behavior", "affordance", "write")
    keywords: tuple[str, ...] = ("affordance", "instructed", "learned", "behavior")

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
        try:
            descriptor = self.service.submit_affordance(args)
        except ValueError as exc:
            structured = {"reason": "invalid_request", "error": str(exc)}
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="affordance submission failed",
                structured=structured,
                llm_text=render_titled_structured_for_llm("Affordance submission failed", structured),
            )
        structured = {
            "affordance_id": descriptor.affordance_id,
            "module_id": descriptor.module_id,
            "title": descriptor.title,
            "source_kind": descriptor.source_kind,
            "scenario_text": descriptor.scenario_text,
            "prompt_hint": descriptor.prompt_hint,
            "capability_refs": list(descriptor.capability_refs),
            "skill_refs": list(descriptor.skill_refs),
            "memory_query_hints": list(descriptor.memory_query_hints),
        }
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text="affordance submitted",
            structured=structured,
            llm_text=render_titled_structured_for_llm("Affordance submitted", structured),
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
