from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
from dataclasses import dataclass, field, replace
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
    BehaviorAdvisorHint,
    BehaviorRouteCandidate,
)
from pal.behavior.decorators import AffordanceBlueprint
from pal.behavior.repository import BehaviorRepository
from pal.behavior.text import canonicalize_affordance_prompt_hint
from pal.foundation import HeatStateRegistry
from pal.foundation.persistence import utc_now
from pal.shared.text_search import jieba_search_terms
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


@dataclass(frozen=True)
class AffordanceTextMatch:
    status: str
    candidates: tuple[AffordanceDescriptor, ...] = ()


@dataclass
class BehaviorService:
    repository: BehaviorRepository = field(default_factory=BehaviorRepository)
    skill_repository: SkillRepository | None = None
    execution_runtime: Any | None = None
    prompt_fragment_registry: Any | None = None
    semantic_router: Callable[..., Any] | None = None
    router_timeout_seconds: float = 2.0
    resident_prompt_budget: int = 5
    advisor_hint_capacity: int = 8
    declared_affordances: dict[str, tuple[AffordanceDescriptor, ...]] = field(default_factory=dict)
    affordance_heat: HeatStateRegistry = field(default_factory=HeatStateRegistry)
    advisor_hints: dict[str, BehaviorAdvisorHint] = field(default_factory=dict)
    advisor_hint_heat: HeatStateRegistry = field(default_factory=HeatStateRegistry)

    async def advise_async(self, request: BehaviorAdviceRequest) -> BehaviorAdviceResult:
        deterministic = self._rank_candidates(request)
        if self.semantic_router is None or not deterministic:
            return self._record_affordance_heat(BehaviorAdviceResult(candidates=tuple(deterministic)))
        try:
            routed = self.semantic_router(request=request, candidates=tuple(deterministic))
            if inspect.isawaitable(routed):
                routed = await asyncio.wait_for(routed, timeout=self.router_timeout_seconds)
            return self._record_affordance_heat(self._normalize_router_result(routed, deterministic))
        except Exception as exc:
            return self._record_affordance_heat(
                BehaviorAdviceResult(
                    candidates=tuple(deterministic),
                    fallback_used=True,
                    router_error=f"{exc.__class__.__name__}: {exc}",
                )
            )

    def update_affordance(self, payload: dict[str, Any]) -> AffordanceDescriptor:
        existing = self.resolve_mutable_affordance(payload)
        updates: dict[str, Any] = {}
        updatable_fields = {
            "scenario_text",
            "prompt_hint",
            "title",
            "activation_terms",
            "capability_refs",
            "skill_refs",
            "memory_query_hints",
            "visibility_mode",
            "activation_kind",
            "activation_mode",
            "source_kind",
            "priority",
            "activation_threshold",
            "enabled",
        }
        for field_name in updatable_fields:
            if field_name in payload and payload[field_name] is not None:
                updates[field_name] = payload[field_name]
        if "scenario_text" in updates:
            updates["scenario_text"] = str(updates["scenario_text"] or "").strip()
            if not updates["scenario_text"]:
                raise ValueError("scenario_text is required")
        if "title" in updates:
            updates["title"] = str(updates["title"] or "").strip()
        if "prompt_hint" in updates:
            effective_title = str(updates.get("title") or existing.title or "").strip()
            updates["prompt_hint"] = canonicalize_affordance_prompt_hint(effective_title, updates["prompt_hint"])
            if not updates["prompt_hint"]:
                raise ValueError("prompt_hint is required")
        if "source_kind" in updates:
            source_kind = str(updates["source_kind"] or "").strip()
            if source_kind not in {AFFORDANCE_SOURCE_INSTRUCTED, AFFORDANCE_SOURCE_LEARNED}:
                raise ValueError("source_kind must be instructed or learned for tool updates")
            updates["source_kind"] = source_kind
        if "visibility_mode" in updates:
            updates["visibility_mode"] = _validated_choice(
                updates["visibility_mode"],
                {AFFORDANCE_VISIBILITY_RESIDENT, AFFORDANCE_VISIBILITY_DISCOVERABLE},
                existing.visibility_mode,
            )
        if "activation_kind" in updates:
            updates["activation_kind"] = _validated_choice(
                updates["activation_kind"],
                {AFFORDANCE_ACTIVATION_DELIBERATIVE, AFFORDANCE_ACTIVATION_REACTIVE},
                existing.activation_kind,
            )
        if "activation_mode" in updates:
            updates["activation_mode"] = _validated_choice(
                updates["activation_mode"],
                {AFFORDANCE_MODE_SUGGEST, AFFORDANCE_MODE_AUTOMATIC, AFFORDANCE_MODE_REQUIRE_APPROVAL},
                existing.activation_mode,
            )
        if "activation_terms" in updates:
            updates["activation_terms"] = _string_tuple(updates["activation_terms"])
        if "capability_refs" in updates:
            updates["capability_refs"] = _string_tuple(updates["capability_refs"])
        if "skill_refs" in updates:
            updates["skill_refs"] = _string_tuple(updates["skill_refs"])
        if "memory_query_hints" in updates:
            updates["memory_query_hints"] = _string_tuple(updates["memory_query_hints"])
        if "priority" in updates:
            updates["priority"] = int(updates["priority"])
        if "activation_threshold" in updates:
            updates["activation_threshold"] = float(updates["activation_threshold"])
        if "enabled" in updates:
            updates["enabled"] = bool(updates["enabled"])
        descriptor = replace(existing, **updates)
        return self.repository.upsert_affordance(descriptor)

    def delete_affordance(self, payload: dict[str, Any]) -> AffordanceDescriptor:
        existing = self.resolve_mutable_affordance(payload)
        if not self.repository.delete_affordance(existing.affordance_id):
            raise ValueError(f"affordance not found: {existing.affordance_id}")
        return existing

    def affordance_text_hash(self, affordance: AffordanceDescriptor) -> str:
        return _affordance_text_hash(affordance)

    def resolve_mutable_affordance(self, payload: dict[str, Any]) -> AffordanceDescriptor:
        affordance_id = str(payload.get("affordance_id") or "").strip()
        if affordance_id:
            existing = self.repository.get_affordance(affordance_id)
            if existing is None:
                raise ValueError(f"affordance not found: {affordance_id}")
            if existing.source_kind == AFFORDANCE_SOURCE_DECLARED:
                raise ValueError(_readonly_affordance_error(existing))
            return existing

        query = str(payload.get("affordance") or payload.get("match_text") or "").strip()
        if not query:
            raise ValueError("affordance text is required")
        match = self.resolve_affordance_by_text(query)
        if match.status == "not_found":
            raise ValueError("affordance not found for provided text")
        if match.status == "ambiguous":
            candidates = ", ".join(
                f"{candidate.affordance_id} [{candidate.source_kind}] hash={_affordance_text_hash(candidate)}"
                for candidate in match.candidates[:5]
            )
            raise ValueError(f"affordance text matched multiple entries: {candidates}")
        if match.status == "readonly":
            descriptor = match.candidates[0]
            raise ValueError(_readonly_affordance_error(descriptor))
        return match.candidates[0]

    def resolve_affordance_by_text(self, text: str) -> "AffordanceTextMatch":
        query_variants = _affordance_query_variants(text)
        if not query_variants:
            return AffordanceTextMatch(status="not_found", candidates=())
        candidates = [
            item
            for item in (*self.repository.list_affordances(), *self._declared_affordance_index().values())
            if item.enabled
        ]
        exact = [
            item
            for item in candidates
            if any(_normalize_affordance_text(_affordance_match_text(item)) == query for query in query_variants)
        ]
        if not exact:
            exact = [item for item in candidates if any(_affordance_text_hash(item) == query for query in query_variants)]
        if not exact:
            exact = [
                item
                for item in candidates
                if _affordance_matches_any_query(item, query_variants)
            ]
        unique = _unique_affordances(exact)
        if not unique:
            return AffordanceTextMatch(status="not_found", candidates=())
        if len(unique) > 1:
            return AffordanceTextMatch(status="ambiguous", candidates=tuple(unique))
        descriptor = unique[0]
        if descriptor.source_kind == AFFORDANCE_SOURCE_DECLARED:
            return AffordanceTextMatch(status="readonly", candidates=(descriptor,))
        return AffordanceTextMatch(status="mutable", candidates=(descriptor,))

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
        prompt_hint = canonicalize_affordance_prompt_hint(title, prompt_hint)
        if not prompt_hint:
            raise ValueError("prompt_hint is required")
        normalized_payload = {**payload, "title": title, "prompt_hint": prompt_hint}
        descriptor = AffordanceDescriptor(
            affordance_id=str(payload.get("affordance_id") or _generated_affordance_id(normalized_payload, source_kind=source_kind)),
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
        self._forget_declared_affordance_heat(module_id)
        self.repository.delete_declared_affordances_for_module(module_id)
        declared_descriptors: list[AffordanceDescriptor] = list(_auto_affordances_from_handle(handle, module_id=module_id))
        resident_descriptors: list[AffordanceDescriptor] = []
        for blueprint in _collect_affordance_blueprints(provider):
            descriptor = _descriptor_from_affordance_blueprint(blueprint, module_id=module_id)
            if descriptor.visibility_mode == AFFORDANCE_VISIBILITY_RESIDENT:
                resident_descriptors.append(descriptor)
            else:
                declared_descriptors.append(descriptor)
        if declared_descriptors:
            self.declared_affordances[module_id] = tuple(declared_descriptors)
        else:
            self.declared_affordances.pop(module_id, None)
        self._register_declared_resident_prompt_provider(module_id, tuple(resident_descriptors))

    def unregister_declared_module(self, module_id: str) -> None:
        self._forget_declared_affordance_heat(module_id)
        self.declared_affordances.pop(module_id, None)
        self.repository.delete_declared_affordances_for_module(module_id)
        self._unregister_declared_resident_prompt_provider(module_id)

    def resident_affordances(self, *, limit: int | None = None) -> tuple[AffordanceDescriptor, ...]:
        affordances = [
            item
            for item in self.repository.list_affordances(enabled_only=True)
            if item.visibility_mode == AFFORDANCE_VISIBILITY_RESIDENT and item.activation_kind == AFFORDANCE_ACTIVATION_DELIBERATIVE
        ]
        affordances.sort(key=_resident_sort_key)
        max_items = self.resident_prompt_budget if limit is None else max(0, int(limit))
        return tuple(affordances[:max_items])

    def hot_affordance_ids(self) -> tuple[str, ...]:
        return self.affordance_heat.hot_keys()

    def hot_affordances(self, *, limit: int | None = None) -> tuple[AffordanceDescriptor, ...]:
        ids = list(self.hot_affordance_ids())
        if not ids:
            return ()
        by_id = {item.affordance_id: item for item in self.repository.list_affordances_by_ids(ids, enabled_only=True)}
        by_id.update(self._declared_affordance_index())
        max_items = len(ids) if limit is None else max(0, int(limit))
        return tuple(by_id[affordance_id] for affordance_id in ids[:max_items] if affordance_id in by_id)

    def tick_affordance_heat(self) -> tuple[str, ...]:
        return tuple(transition.key for transition in self.affordance_heat.tick() if transition.expired)

    def active_advisor_hints(self, *, limit: int | None = None) -> tuple[BehaviorAdvisorHint, ...]:
        hot_ids = set(self.advisor_hint_heat.hot_keys())
        if not hot_ids:
            return ()
        hints = [hint for hint_id, hint in self.advisor_hints.items() if hint_id in hot_ids]
        hints.sort(key=lambda item: (item.touched_at, item.hint_id), reverse=True)
        max_items = self.advisor_hint_capacity if limit is None else max(0, int(limit))
        return tuple(hints[:max_items])

    def tick_advisor_hints(self) -> tuple[str, ...]:
        expired: list[str] = []
        for transition in self.advisor_hint_heat.tick():
            if transition.expired:
                expired.append(transition.key)
                self.advisor_hints.pop(transition.key, None)
        return tuple(expired)

    def _register_declared_resident_prompt_provider(self, module_id: str, affordances: tuple[AffordanceDescriptor, ...]) -> None:
        registry = self.prompt_fragment_registry
        if registry is None:
            return
        from pal.behavior.prompt import DeclaredResidentAffordancePromptFragmentProvider, declared_resident_affordance_provider_id

        registry.unregister(declared_resident_affordance_provider_id(module_id))
        if affordances:
            registry.register(DeclaredResidentAffordancePromptFragmentProvider(module_id=module_id, affordances=affordances))

    def _unregister_declared_resident_prompt_provider(self, module_id: str) -> None:
        registry = self.prompt_fragment_registry
        if registry is None:
            return
        from pal.behavior.prompt import declared_resident_affordance_provider_id

        registry.unregister(declared_resident_affordance_provider_id(module_id))

    def _record_affordance_heat(self, result: BehaviorAdviceResult) -> BehaviorAdviceResult:
        for candidate in result.candidates:
            self.affordance_heat.promote_to_hot(candidate.affordance_id)
        self._record_advisor_hints(result.candidates)
        return result

    def _record_advisor_hints(self, candidates: Iterable[BehaviorRouteCandidate]) -> None:
        now = utc_now()
        for candidate in candidates:
            hint = _advisor_hint_from_candidate(candidate, now=now, existing=self.advisor_hints.get(candidate.affordance_id))
            if not hint.rendered:
                continue
            self.advisor_hints[hint.hint_id] = hint
            self.advisor_hint_heat.promote_to_hot(hint.hint_id)
        self._evict_advisor_hint_overflow()

    def _evict_advisor_hint_overflow(self) -> None:
        capacity = max(0, int(self.advisor_hint_capacity or 0))
        while len(self.advisor_hints) > capacity:
            oldest = min(self.advisor_hints.values(), key=lambda item: (item.touched_at, item.hint_id))
            self.advisor_hints.pop(oldest.hint_id, None)
            self.advisor_hint_heat.remove(oldest.hint_id)

    def _forget_declared_affordance_heat(self, module_id: str) -> None:
        for descriptor in self.declared_affordances.get(str(module_id), ()):
            self.affordance_heat.remove(descriptor.affordance_id)

    def _rank_candidates(self, request: BehaviorAdviceRequest) -> list[BehaviorRouteCandidate]:
        candidates: list[BehaviorRouteCandidate] = []
        top_k = max(0, int(request.top_k or 0))
        if top_k <= 0:
            return candidates
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
        candidate_limit = max(top_k * 6, 20)
        retrieval_scores, _ = self.repository.collect_route_candidates(query_text, limit=candidate_limit)
        retrieval_scores = _merge_scores(retrieval_scores, self._declared_route_scores(query_text))
        retrieval_scores = _prune_weak_retrieval_scores(retrieval_scores)
        by_id = {item.affordance_id: item for item in self.repository.list_affordances_by_ids(retrieval_scores.keys(), enabled_only=True)}
        by_id.update(self._declared_affordance_index())
        for affordance_id in retrieval_scores.keys():
            affordance = by_id.get(affordance_id)
            if affordance is None or not affordance.enabled:
                continue
            if affordance.affordance_id in already_considered:
                continue
            if _would_recurse(affordance, already_considered):
                continue
            skill_refs = tuple(skill_id for skill_id in affordance.skill_refs if skill_id in enabled_skills)
            if affordance.skill_refs and not skill_refs and not affordance.capability_refs and not affordance.memory_query_hints:
                continue
            relevance = _fts_relevance(retrieval_scores.get(affordance.affordance_id, 0.0))
            if relevance <= 0.0:
                continue
            confidence = _confidence(relevance=relevance, source_kind=affordance.source_kind, priority=affordance.priority)
            if confidence < affordance.activation_threshold:
                continue
            prompt_hint = affordance.prompt_hint.strip()
            reason = _reason_for(affordance, relevance=relevance)
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
        return candidates[:top_k]

    def _declared_affordance_index(self) -> dict[str, AffordanceDescriptor]:
        by_id: dict[str, AffordanceDescriptor] = {}
        for descriptors in self.declared_affordances.values():
            for descriptor in descriptors:
                by_id[descriptor.affordance_id] = descriptor
        return by_id

    def _declared_route_scores(self, query_text: str) -> dict[str, float]:
        query_tokens = _tokenize(query_text)
        if not query_tokens:
            return {}
        normalized_query = str(query_text or "").lower()
        scores: dict[str, float] = {}
        for affordance in self._declared_affordance_index().values():
            if not affordance.enabled:
                continue
            candidate_text = _declared_route_score_text(affordance)
            candidate_tokens = _tokenize(candidate_text)
            overlap = query_tokens & candidate_tokens
            activation_terms = {str(term).lower() for term in affordance.activation_terms if str(term).strip()}
            activation_hits = query_tokens & activation_terms
            phrase_hits = sum(1 for term in activation_terms if len(term) >= 3 and term in normalized_query)
            score = float(len(overlap) + len(activation_hits) * 2 + phrase_hits * 3)
            if score > 0.0:
                scores[affordance.affordance_id] = score
        return scores

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
            return BehaviorAdviceResult(
                candidates=_merge_routed_candidates(routed.candidates, deterministic),
                fallback_used=routed.fallback_used,
                router_error=routed.router_error,
            )
        if routed is None:
            return BehaviorAdviceResult(candidates=tuple(deterministic))
        if isinstance(routed, Sequence) and not isinstance(routed, (str, bytes, bytearray)):
            candidates = _merge_routed_candidates(routed, deterministic)
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
        prompt_hint=canonicalize_affordance_prompt_hint(blueprint.title, blueprint.prompt_hint),
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
                activation_terms=_token_tuple(" ".join((name, canonical, display, family, namespace))),
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


def _declared_route_score_text(affordance: AffordanceDescriptor) -> str:
    if bool(affordance.metadata.get("auto_generated")):
        return " ".join(
            (
                affordance.title,
                " ".join(affordance.activation_terms),
                " ".join(affordance.capability_refs),
                " ".join(affordance.skill_refs),
                " ".join(affordance.memory_query_hints),
            )
        )
    return " ".join(
        (
            affordance.title,
            affordance.scenario_text,
            affordance.prompt_hint,
            " ".join(affordance.activation_terms),
            " ".join(affordance.capability_refs),
            " ".join(affordance.skill_refs),
            " ".join(affordance.memory_query_hints),
        )
    )


def _tokenize(text: str) -> set[str]:
    return set(jieba_search_terms(str(text or "").lower()))


def _fts_relevance(score: float) -> float:
    if score <= 0.0:
        return 0.0
    if score >= 1.0:
        return min(1.0, score / 10.0)
    return min(1.0, 0.18 + math.log1p(score * 1_000_000.0) / 10.0)


def _prune_weak_retrieval_scores(scores: dict[str, float]) -> dict[str, float]:
    if len(scores) <= 1:
        return scores
    best = max(scores.values())
    if best <= 0.0:
        return {}
    cutoff = best * 0.75
    return {key: value for key, value in scores.items() if value >= cutoff}


def _merge_scores(*score_maps: dict[str, float]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for scores in score_maps:
        for key, value in scores.items():
            merged[key] = max(merged.get(key, 0.0), float(value))
    return merged


def _merge_routed_candidates(
    routed: Sequence[Any],
    deterministic: list[BehaviorRouteCandidate],
) -> tuple[BehaviorRouteCandidate, ...]:
    deterministic_by_id = {candidate.affordance_id: candidate for candidate in deterministic}
    candidates: list[BehaviorRouteCandidate] = []
    seen: set[str] = set()
    for item in routed:
        if not isinstance(item, BehaviorRouteCandidate):
            continue
        affordance_id = item.affordance_id
        if affordance_id not in deterministic_by_id or affordance_id in seen:
            continue
        seen.add(affordance_id)
        candidates.append(item)
    for candidate in deterministic:
        if candidate.affordance_id in seen:
            continue
        seen.add(candidate.affordance_id)
        candidates.append(candidate)
    return tuple(candidates)


def _confidence(*, relevance: float, source_kind: str, priority: int) -> float:
    source_prior = SOURCE_PRIORS.get(source_kind, 0.5)
    priority_component = max(0.0, min(1.0, float(priority) / 100.0))
    return min(1.0, relevance * 0.62 + source_prior * 0.28 + priority_component * 0.10)


def _reason_for(affordance: AffordanceDescriptor, *, relevance: float) -> str:
    source = affordance.source_kind
    if source == AFFORDANCE_SOURCE_INSTRUCTED:
        return f"User-instructed affordance matched this scenario with FTS relevance {relevance:.2f}."
    if source == AFFORDANCE_SOURCE_DECLARED:
        return f"Declared affordance matched this scenario with FTS relevance {relevance:.2f}."
    return f"Learned affordance may match this scenario with FTS relevance {relevance:.2f}."


def _would_recurse(affordance: AffordanceDescriptor, already_considered: set[str]) -> bool:
    recursive_refs = {"op_behavior_advise", "op_behavior_advise", "behavior:advise"}
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


def _advisor_hint_from_candidate(
    candidate: BehaviorRouteCandidate,
    *,
    now: str,
    existing: BehaviorAdvisorHint | None,
) -> BehaviorAdvisorHint:
    rendered = _render_advisor_hint_body(candidate)
    return BehaviorAdvisorHint(
        hint_id=candidate.affordance_id,
        title=candidate.title or candidate.affordance_id,
        rendered=rendered,
        source_ref=candidate.affordance_id,
        payload=candidate.to_dict(),
        created_at=existing.created_at if existing is not None and existing.created_at else now,
        touched_at=now,
    )


def _render_advisor_hint_body(candidate: BehaviorRouteCandidate) -> str:
    parts: list[str] = []
    if candidate.prompt_hint.strip():
        parts.append(f"Hint: {candidate.prompt_hint.strip()}")
    if candidate.skill_refs:
        parts.append(
            "Skill refs: "
            + ", ".join(candidate.skill_refs)
            + ". MUST NOT call `skill_inject` solely because listed; call it only when workflow/domain rules are needed."
        )
    if candidate.capability_refs:
        parts.append(
            "Capability refs: "
            + ", ".join(candidate.capability_refs)
            + ". Resolve current inventory before use; if one directly completes the request, use it without injecting a skill."
        )
    if candidate.memory_query_hints:
        parts.append(
            "Memory query hints: "
            + ", ".join(candidate.memory_query_hints)
            + ". They do not trigger recall by themselves; when recall is required, use them as query seeds."
        )
    return " ".join(parts).strip()


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


def _affordance_match_text(affordance: AffordanceDescriptor) -> str:
    return "\n".join(
        part
        for part in (
            affordance.title,
            affordance.scenario_text,
            affordance.prompt_hint,
        )
        if str(part or "").strip()
    )


def _normalize_affordance_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _affordance_query_variants(value: object) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return ()
    variants = [raw]
    variants.append(_strip_xml_tags(raw))
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        variants.append(cleaned)
        without_bullet = re.sub(r"^\s*[-*•]\s+", "", cleaned).strip()
        variants.append(without_bullet)
        title_split = _split_rendered_affordance_line(without_bullet)
        if title_split:
            variants.append(title_split)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in variants:
        candidate = _normalize_affordance_text(item)
        if candidate and candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return tuple(normalized)


def _strip_xml_tags(value: str) -> str:
    return re.sub(r"</?[^>\n]+>", " ", str(value or ""))


def _split_rendered_affordance_line(value: str) -> str:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return ""
    title, remainder = text.split(":", 1)
    if len(title.strip()) > 100:
        return ""
    return remainder.strip()


def _affordance_matches_any_query(affordance: AffordanceDescriptor, queries: tuple[str, ...]) -> bool:
    fields = (
        _normalize_affordance_text(_affordance_match_text(affordance)),
        _normalize_affordance_text(_rendered_affordance_text(affordance)),
        _normalize_affordance_text(affordance.prompt_hint),
        _normalize_affordance_text(affordance.scenario_text),
        _normalize_affordance_text(affordance.title),
    )
    for query in queries:
        for field in fields:
            if not query or not field:
                continue
            if query == field or query in field or field in query:
                return True
    return False


def _rendered_affordance_text(affordance: AffordanceDescriptor) -> str:
    title = affordance.title.strip()
    hint = affordance.prompt_hint.strip()
    if title and hint:
        return f"{title}: {hint}"
    return title or hint


def _affordance_text_hash(affordance: AffordanceDescriptor) -> str:
    return hashlib.sha256(_normalize_affordance_text(_affordance_match_text(affordance)).encode("utf-8")).hexdigest()[:16]


def _unique_affordances(items: Iterable[AffordanceDescriptor]) -> list[AffordanceDescriptor]:
    seen: set[str] = set()
    unique: list[AffordanceDescriptor] = []
    for item in items:
        if item.affordance_id in seen:
            continue
        seen.add(item.affordance_id)
        unique.append(item)
    return unique


def _readonly_affordance_error(affordance: AffordanceDescriptor) -> str:
    return (
        "readonly injected affordance; behavior update/delete only changes persisted database affordances. "
        f"source_kind={affordance.source_kind}, module_id={affordance.module_id}, hash={_affordance_text_hash(affordance)}"
    )


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
