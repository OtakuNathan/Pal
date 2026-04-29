from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from pal.behavior.contracts import (
    AFFORDANCE_ACTIVATION_DELIBERATIVE,
    AFFORDANCE_ACTIVATION_REACTIVE,
    AFFORDANCE_AVAILABLE,
    AFFORDANCE_MODE_AUTOMATIC,
    AFFORDANCE_MODE_REQUIRE_APPROVAL,
    AFFORDANCE_MODE_SUGGEST,
    AFFORDANCE_PARTIAL,
    AFFORDANCE_SOURCE_DECLARED,
    AFFORDANCE_SOURCE_INSTRUCTED,
    AFFORDANCE_SOURCE_LEARNED,
    AFFORDANCE_UNAVAILABLE,
    AFFORDANCE_VISIBILITY_DISCOVERABLE,
    AFFORDANCE_VISIBILITY_RESIDENT,
    AffordanceDescriptor,
    BehaviorAdviceRequest,
    BehaviorAdviceResult,
    BehaviorRouteCandidate,
)
from pal.behavior.decorators import AffordanceBlueprint
from pal.behavior.repository import BehaviorRepository
from pal.foundation.persistence import utc_now
from pal.skill.repository import SkillRepository


SOURCE_PRIORS = {
    AFFORDANCE_SOURCE_INSTRUCTED: 0.92,
    AFFORDANCE_SOURCE_DECLARED: 0.76,
    AFFORDANCE_SOURCE_LEARNED: 0.52,
}

SOURCE_SORT_ORDER = {
    AFFORDANCE_SOURCE_INSTRUCTED: 0,
    AFFORDANCE_SOURCE_DECLARED: 1,
    AFFORDANCE_SOURCE_LEARNED: 2,
}


@dataclass
class BehaviorService:
    repository: BehaviorRepository = field(default_factory=BehaviorRepository)
    skill_repository: SkillRepository | None = None
    execution_runtime: Any | None = None
    semantic_router: Callable[..., Any] | None = None
    router_timeout_seconds: float = 2.0
    resident_prompt_budget: int = 5

    async def advise_async(self, request: BehaviorAdviceRequest) -> BehaviorAdviceResult:
        deterministic = self._rank_candidates(request)
        if self.semantic_router is None or not deterministic:
            return BehaviorAdviceResult(candidates=tuple(deterministic))
        try:
            routed = self.semantic_router(request=request, candidates=tuple(deterministic))
            if inspect.isawaitable(routed):
                routed = await asyncio.wait_for(routed, timeout=self.router_timeout_seconds)
            return self._normalize_router_result(routed, deterministic)
        except Exception as exc:
            return BehaviorAdviceResult(
                candidates=tuple(deterministic),
                fallback_used=True,
                router_error=f"{exc.__class__.__name__}: {exc}",
            )

    def submit_affordance(self, payload: dict[str, Any]) -> AffordanceDescriptor:
        scenario_text = str(payload.get("scenario_text") or "").strip()
        prompt_hint = str(payload.get("prompt_hint") or "").strip()
        if not scenario_text:
            raise ValueError("scenario_text is required")
        if not prompt_hint:
            raise ValueError("prompt_hint is required")
        source_kind = str(payload.get("source_kind") or AFFORDANCE_SOURCE_INSTRUCTED).strip()
        if source_kind not in {AFFORDANCE_SOURCE_INSTRUCTED, AFFORDANCE_SOURCE_LEARNED}:
            raise ValueError("source_kind must be instructed or learned for tool submissions")
        visibility_mode = _validated_choice(
            payload.get("visibility_mode"),
            {AFFORDANCE_VISIBILITY_RESIDENT, AFFORDANCE_VISIBILITY_DISCOVERABLE},
            AFFORDANCE_VISIBILITY_DISCOVERABLE,
        )
        activation_kind = _validated_choice(
            payload.get("activation_kind"),
            {AFFORDANCE_ACTIVATION_DELIBERATIVE, AFFORDANCE_ACTIVATION_REACTIVE},
            AFFORDANCE_ACTIVATION_DELIBERATIVE,
        )
        activation_mode = _validated_choice(
            payload.get("activation_mode"),
            {AFFORDANCE_MODE_SUGGEST, AFFORDANCE_MODE_AUTOMATIC, AFFORDANCE_MODE_REQUIRE_APPROVAL},
            AFFORDANCE_MODE_SUGGEST,
        )
        title = str(payload.get("title") or scenario_text[:80] or "Untitled affordance").strip()
        descriptor = AffordanceDescriptor(
            affordance_id=str(payload.get("affordance_id") or _generated_affordance_id(payload, source_kind=source_kind)),
            module_id="behavior",
            title=title,
            scenario_text=scenario_text,
            prompt_hint=prompt_hint,
            visibility_mode=visibility_mode,
            activation_kind=activation_kind,
            activation_mode=activation_mode,
            source_kind=source_kind,
            activation_terms=_string_tuple(payload.get("activation_terms")),
            capability_refs=_string_tuple(payload.get("capability_refs")),
            skill_refs=_string_tuple(payload.get("skill_refs")),
            memory_query_hints=_string_tuple(payload.get("memory_query_hints")),
            priority=int(payload.get("priority") or 100),
            activation_threshold=float(payload.get("activation_threshold") or 0.25),
            enabled=bool(payload.get("enabled", True)),
            metadata=dict(payload.get("metadata") or {}),
        )
        return self.repository.upsert_affordance(descriptor)

    def register_declared_module(self, handle: Any) -> None:
        provider = getattr(handle, "introspection_provider", None)
        if provider is None:
            return
        module_id = str(getattr(handle, "module_id", "") or getattr(provider, "module_id", "") or "")
        if not module_id:
            return
        for descriptor in _auto_affordances_from_handle(handle, module_id=module_id):
            self.repository.upsert_affordance(descriptor)
        for blueprint in _collect_affordance_blueprints(provider):
            self.repository.upsert_affordance(_descriptor_from_affordance_blueprint(blueprint, module_id=module_id))

    def unregister_declared_module(self, module_id: str) -> None:
        self.repository.delete_declared_affordances_for_module(module_id)

    def resident_affordances(self, *, limit: int | None = None) -> tuple[AffordanceDescriptor, ...]:
        affordances = [
            item
            for item in self.repository.list_affordances(enabled_only=True)
            if item.visibility_mode == AFFORDANCE_VISIBILITY_RESIDENT and item.activation_kind == AFFORDANCE_ACTIVATION_DELIBERATIVE
        ]
        affordances.sort(key=_resident_sort_key)
        max_items = self.resident_prompt_budget if limit is None else max(0, int(limit))
        return tuple(affordances[:max_items])

    def _rank_candidates(self, request: BehaviorAdviceRequest) -> list[BehaviorRouteCandidate]:
        candidates: list[BehaviorRouteCandidate] = []
        repository = self.skill_repository or self.repository.skill_repository
        enabled_skills = {skill.skill_id for skill in repository.list_skills(active_only=True)}
        already_considered = {str(item).strip() for item in request.already_considered if str(item).strip()}
        query_text = " ".join(
            part
            for part in (
                request.scenario,
                request.intent,
                request.turn_kind,
                " ".join(request.constraints),
            )
            if part
        )
        query_tokens = _tokenize(query_text)
        for affordance in self.repository.list_affordances(enabled_only=True):
            if affordance.affordance_id in already_considered:
                continue
            if _would_recurse(affordance, already_considered):
                continue
            skill_refs = tuple(skill_id for skill_id in affordance.skill_refs if skill_id in enabled_skills)
            if affordance.skill_refs and not skill_refs and not affordance.capability_refs and not affordance.memory_query_hints:
                continue
            lexical = _lexical_score(
                query_tokens,
                (
                    affordance.title,
                    affordance.scenario_text,
                    affordance.prompt_hint,
                    " ".join(affordance.activation_terms),
                ),
            )
            confidence = _confidence(lexical=lexical, source_kind=affordance.source_kind, priority=affordance.priority)
            if confidence < affordance.activation_threshold:
                continue
            prompt_hint = affordance.prompt_hint.strip()
            reason = _reason_for(affordance, lexical=lexical)
            if affordance.source_kind == AFFORDANCE_SOURCE_LEARNED:
                prompt_hint = _weaken_learned_text(prompt_hint)
                reason = _weaken_learned_text(reason)
            candidates.append(
                BehaviorRouteCandidate(
                    affordance_id=affordance.affordance_id,
                    title=affordance.title,
                    confidence=round(confidence, 4),
                    availability=self._availability(affordance.capability_refs),
                    reason=reason,
                    prompt_hint=prompt_hint,
                    visibility_mode=affordance.visibility_mode,
                    activation_kind=affordance.activation_kind,
                    activation_mode=affordance.activation_mode,
                    source_kind=affordance.source_kind,
                    capability_refs=affordance.capability_refs,
                    skill_refs=skill_refs,
                    memory_query_hints=affordance.memory_query_hints,
                    evidence_refs=affordance.evidence_refs,
                    metadata=dict(affordance.metadata),
                )
            )
        candidates.sort(key=_candidate_sort_key)
        top_k = max(0, int(request.top_k or 0))
        return candidates[:top_k]

    def _availability(self, capability_refs: Sequence[str]) -> str:
        refs = tuple(ref for ref in capability_refs if str(ref).strip())
        if not refs:
            return AFFORDANCE_AVAILABLE
        runtime = self.execution_runtime
        if runtime is None or not hasattr(runtime, "get_capability_spec"):
            return AFFORDANCE_UNAVAILABLE
        available = 0
        for ref in refs:
            if runtime.get_capability_spec(ref) is not None:
                available += 1
        if available == len(refs):
            return AFFORDANCE_AVAILABLE
        if available > 0:
            return AFFORDANCE_PARTIAL
        return AFFORDANCE_UNAVAILABLE

    def _normalize_router_result(
        self,
        routed: Any,
        deterministic: list[BehaviorRouteCandidate],
    ) -> BehaviorAdviceResult:
        if isinstance(routed, BehaviorAdviceResult):
            return routed
        if routed is None:
            return BehaviorAdviceResult(candidates=tuple(deterministic))
        if isinstance(routed, Sequence) and not isinstance(routed, (str, bytes, bytearray)):
            candidates = tuple(item for item in routed if isinstance(item, BehaviorRouteCandidate))
            if candidates:
                return BehaviorAdviceResult(candidates=candidates)
        return BehaviorAdviceResult(candidates=tuple(deterministic))


def _collect_affordance_blueprints(provider: Any) -> tuple[AffordanceBlueprint, ...]:
    collected: list[AffordanceBlueprint] = []
    collected.extend(getattr(provider.__class__, "__behavior_affordance_blueprints__", ()))
    for _, value in inspect.getmembers(provider.__class__):
        collected.extend(getattr(value, "__behavior_affordance_blueprints__", ()))
    return tuple(collected)


def _descriptor_from_affordance_blueprint(blueprint: AffordanceBlueprint, *, module_id: str) -> AffordanceDescriptor:
    now = utc_now()
    return AffordanceDescriptor(
        affordance_id=blueprint.affordance_id,
        module_id=module_id,
        title=blueprint.title,
        scenario_text=blueprint.scenario_text,
        prompt_hint=blueprint.prompt_hint,
        visibility_mode=blueprint.visibility_mode,
        activation_kind=blueprint.activation_kind,
        activation_mode=blueprint.activation_mode,
        source_kind=blueprint.source_kind,
        activation_terms=blueprint.activation_terms,
        capability_refs=blueprint.capability_refs,
        skill_refs=blueprint.skill_refs,
        memory_query_hints=blueprint.memory_query_hints,
        priority=blueprint.priority,
        activation_threshold=blueprint.activation_threshold,
        enabled=blueprint.enabled,
        metadata=blueprint.metadata,
        created_at=now,
        updated_at=now,
    )


def _auto_affordances_from_handle(handle: Any, *, module_id: str) -> tuple[AffordanceDescriptor, ...]:
    subtree = getattr(handle, "mounted_subtree", None)
    descriptors = tuple(getattr(subtree, "descriptors", ()) or ())
    now = utc_now()
    generated: list[AffordanceDescriptor] = []
    for descriptor in descriptors:
        canonical = str(getattr(descriptor, "canonical_path", "") or getattr(descriptor, "name", "") or "").strip()
        name = str(getattr(descriptor, "name", "") or canonical).strip()
        if not name:
            continue
        description = str(getattr(descriptor, "description", "") or "").strip()
        display = str(getattr(descriptor, "display_name", "") or canonical or name).strip()
        family = str(getattr(descriptor, "family", "") or "capability").strip()
        namespace = str((getattr(descriptor, "metadata", {}) or {}).get("namespace") or family).strip()
        target_label = str(getattr(descriptor, "target_label", "") or "").strip()
        title = display.replace("_", " ")
        scenario_parts = [title, description, family, namespace, target_label]
        scenario_text = ". ".join(part for part in scenario_parts if part)
        generated.append(
            AffordanceDescriptor(
                affordance_id=f"declared.capability.{module_id}.{_safe_id(name)}",
                module_id=module_id,
                title=title,
                scenario_text=scenario_text,
                prompt_hint=f"Consider capability `{name}` when this scenario matches: {description or title}.",
                visibility_mode=AFFORDANCE_VISIBILITY_DISCOVERABLE,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                activation_mode=AFFORDANCE_MODE_SUGGEST,
                source_kind=AFFORDANCE_SOURCE_DECLARED,
                activation_terms=_token_tuple(" ".join((name, canonical, display, description, family, namespace))),
                capability_refs=(name,),
                priority=45 if namespace == "introspection" else 55,
                activation_threshold=0.35,
                enabled=True,
                metadata={"auto_generated": True, "capability_name": name, "canonical_path": canonical},
                created_at=now,
                updated_at=now,
            )
        )
    return tuple(generated)


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[A-Za-z0-9_\-.]+|[一-鿿]+", str(text).lower()) if token}


def _lexical_score(query_tokens: set[str], fields: Iterable[str]) -> float:
    haystack_tokens = _tokenize(" ".join(str(field or "") for field in fields))
    if not query_tokens or not haystack_tokens:
        return 0.0
    overlap = len(query_tokens & haystack_tokens)
    if overlap <= 0:
        return 0.0
    return min(1.0, overlap / max(1, min(len(query_tokens), len(haystack_tokens))))


def _confidence(*, lexical: float, source_kind: str, priority: int) -> float:
    source_prior = SOURCE_PRIORS.get(source_kind, 0.5)
    priority_component = max(0.0, min(1.0, float(priority) / 100.0))
    return min(1.0, lexical * 0.62 + source_prior * 0.28 + priority_component * 0.10)


def _reason_for(affordance: AffordanceDescriptor, *, lexical: float) -> str:
    source = affordance.source_kind
    if source == AFFORDANCE_SOURCE_INSTRUCTED:
        return f"User-instructed affordance matched this scenario with lexical score {lexical:.2f}."
    if source == AFFORDANCE_SOURCE_DECLARED:
        return f"Declared affordance matched this scenario with lexical score {lexical:.2f}."
    return f"Learned affordance may match this scenario with lexical score {lexical:.2f}."


def _would_recurse(affordance: AffordanceDescriptor, already_considered: set[str]) -> bool:
    recursive_refs = {"op_behavior_advise", "behavior_advise", "behavior:advise"}
    refs = {affordance.affordance_id, *affordance.capability_refs, *affordance.skill_refs}
    return bool((refs & recursive_refs) or (refs & already_considered))


def _weaken_learned_text(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return "Consider this learned affordance as a weak hint."
    replacements = {
        "must": "can consider",
        "always": "can consider",
        "definitely": "may",
        "should": "can consider",
        "required": "possible",
    }
    words = normalized.split()
    softened = " ".join(replacements.get(word.lower().strip(".,;:!"), word) for word in words)
    if not softened.lower().startswith(("consider", "maybe", "may")):
        softened = f"Consider: {softened}"
    return softened


def _candidate_sort_key(candidate: BehaviorRouteCandidate) -> tuple[float, float, int, str]:
    return (
        -candidate.confidence,
        -SOURCE_PRIORS.get(candidate.source_kind, 0.5),
        SOURCE_SORT_ORDER.get(candidate.source_kind, 99),
        candidate.affordance_id,
    )


def _resident_sort_key(affordance: AffordanceDescriptor) -> tuple[int, int, str, str]:
    return (
        SOURCE_SORT_ORDER.get(affordance.source_kind, 99),
        -int(affordance.priority),
        str(affordance.updated_at or ""),
        affordance.affordance_id,
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _validated_choice(value: object, allowed: set[str], default: str) -> str:
    candidate = str(value or default).strip()
    if candidate not in allowed:
        raise ValueError(f"unsupported value: {candidate}")
    return candidate


def _generated_affordance_id(payload: dict[str, Any], *, source_kind: str) -> str:
    pieces = [
        source_kind,
        str(payload.get("scenario_text") or ""),
        str(payload.get("prompt_hint") or ""),
        "|".join(_string_tuple(payload.get("capability_refs"))),
        "|".join(_string_tuple(payload.get("skill_refs"))),
    ]
    digest = hashlib.sha1("\n".join(pieces).encode("utf-8")).hexdigest()[:12]
    return f"{source_kind}.affordance.{digest}"


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", ".", value).strip(".")
    return safe or hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _token_tuple(text: str) -> tuple[str, ...]:
    return tuple(sorted(_tokenize(text)))
