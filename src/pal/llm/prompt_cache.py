from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from pal.llm import cache_handoff as handoff

from pal.llm.ir import (
    LLMRequestIR,
    LLMUsageIR,
    MessageState,
    PromptRegionIR,
    WireShape,
)
from pal.llm.cache_diagnostics import describe_request, compare_requests
from pal.llm.shapes.base import EncodedMessageSpan, EncodedRequest, ShapeContext
from pal.shared.json_values import thaw_json

logger = logging.getLogger(__name__)


class PromptCacheDialect(StrEnum):
    NONE = "none"
    OPENAI_RESPONSES_EXPLICIT = "openai_responses_explicit"
    OPENAI_CHAT_EXPLICIT = "openai_chat_explicit"
    OPENAI_AUTOMATIC = "openai_automatic"
    OPENROUTER_OPENAI_EXPLICIT = "openrouter_openai_explicit"
    OPENROUTER_AUTOMATIC = "openrouter_automatic"
    OPENROUTER_ANTHROPIC_AUTOMATIC = "openrouter_anthropic_automatic"
    OPENROUTER_ANTHROPIC_EXPLICIT = "openrouter_anthropic_explicit"
    ANTHROPIC_EXPLICIT = "anthropic_explicit"


@dataclass(frozen=True)
class CacheProfile:
    """Narrow, immutable cache strategy selection for one provider family.

    Selection only: a profile chooses a dialect and wire flags. It owns no
    cache state, no secrets, and no planner internals. Endpoints without a
    profile keep the default (legacy) resolution unchanged.
    """

    profile_id: str
    strategy: str
    dialect: PromptCacheDialect
    allow_explicit_breakpoints: bool = True
    allow_stable_anchor_marker: bool = False
    stable_prompt_cache_key: bool = True
    stable_session_id: bool = True
    telemetry_usage_required: bool = True
    version: str = "2"


# Canary comparison groups from the same-turn cache plan. The default for a
# matching endpoint stays legacy until a paid canary picks a winner; switching
# is a configuration action, never an automatic behavior.
CACHE_PROFILES: Mapping[str, CacheProfile] = MappingProxyType({
    handoff.POLICY: CacheProfile(handoff.POLICY, handoff.POLICY,
        PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT, version="1"),
    "openrouter_astra_legacy_explicit": CacheProfile(
        profile_id="openrouter_astra_legacy_explicit",
        strategy="legacy_explicit",
        dialect=PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
    ),
    "openrouter_astra_provider_implicit": CacheProfile(
        profile_id="openrouter_astra_provider_implicit",
        strategy="provider_implicit",
        dialect=PromptCacheDialect.OPENROUTER_AUTOMATIC,
        allow_explicit_breakpoints=False,
    ),
    "openrouter_astra_hybrid_anchor": CacheProfile(
        profile_id="openrouter_astra_hybrid_anchor",
        strategy="hybrid_anchor",
        dialect=PromptCacheDialect.OPENROUTER_AUTOMATIC,
        allow_explicit_breakpoints=False,
        allow_stable_anchor_marker=True,
    ),
})


@dataclass(frozen=True)
class PromptCacheBreakpoint:
    label: str
    message_id: str
    ttl: str = "5m"
    prefix_tokens: int = 0


@dataclass(frozen=True)
class PromptCacheTrackPlan:
    name: str
    ttl: str
    decision: str = "unavailable"
    epoch_key: str = ""
    submitted_message_id: str = ""
    submitted_fingerprint: str = ""
    submitted_prefix_tokens: int = 0
    target_message_id: str = ""
    target_fingerprint: str = ""
    target_prefix_tokens: int = 0
    candidate_message_id: str = ""
    planning_base_tokens: int = 0
    reprocessed_delta_tokens: int = 0
    projected_reprocessed_tokens: int = 0
    estimated_net_tokens: float = 0.0


    @property
    def confirmed_message_id(self) -> str:
        return ""

    @property
    def confirmed_prefix_tokens(self) -> int:
        return 0


@dataclass(frozen=True)
class PromptCachePlan:
    scope_key: str
    cache_key: str
    dialect: PromptCacheDialect
    breakpoints: tuple[PromptCacheBreakpoint, ...] = ()
    decision: str = "disabled"
    estimated_prefix_tokens: int = 0
    plan_sequence: int = 0
    profile_id: str = ""
    strategy: str = ""
    profile_version: str = ""
    profile_origin: str = "default"
    profile_generation: str = ""
    allow_stable_anchor_marker: bool = False
    handoff_encoded: EncodedRequest | None = field(default=None, repr=False, compare=False)
    handoff_epoch: int = 0
    handoff_owner: int = 0
    handoff_candidate: str = ""
    anchor: PromptCacheTrackPlan = field(
        default_factory=lambda: PromptCacheTrackPlan("anchor", "5m")
    )
    frontier: PromptCacheTrackPlan = field(
        default_factory=lambda: PromptCacheTrackPlan("frontier", "5m")
    )

    @property
    def enabled(self) -> bool:
        return self.dialect != PromptCacheDialect.NONE

    @property
    def submitted_message_id(self) -> str:
        return self.frontier.submitted_message_id or self.anchor.submitted_message_id

    @property
    def candidate_message_id(self) -> str:
        return self.anchor.candidate_message_id or self.frontier.candidate_message_id

    @property
    def submitted_prefix_tokens(self) -> int:
        return max(
            self.anchor.submitted_prefix_tokens,
            self.frontier.submitted_prefix_tokens,
        )

    @property
    def candidate_prefix_tokens(self) -> int:
        return max(
            self.anchor.target_prefix_tokens,
            self.frontier.target_prefix_tokens,
        )

    @property
    def reprocessed_delta_tokens(self) -> int:
        return max(
            self.anchor.reprocessed_delta_tokens,
            self.frontier.reprocessed_delta_tokens,
        )

    @property
    def projected_reprocessed_tokens(self) -> int:
        return max(
            self.anchor.projected_reprocessed_tokens,
            self.frontier.projected_reprocessed_tokens,
        )

    @property
    def estimated_net_tokens(self) -> float:
        return max(
            self.anchor.estimated_net_tokens,
            self.frontier.estimated_net_tokens,
        )


    @property
    def confirmed_message_id(self) -> str:
        return ""

    @property
    def confirmed_prefix_tokens(self) -> int:
        return 0


@dataclass
class _TrackStats:
    submitted_message_id: str = ""
    submitted_fingerprint: str = ""
    submitted_prefix_tokens: int = 0
    submitted_sequence: int = 0
    last_recorded_sequence: int = 0
    accumulated_reprocessed_tokens: int = 0
    last_decision: str = ""
    submitted_at: float = 0.0

    def clear(self) -> None:
        self.submitted_message_id = ""
        self.submitted_fingerprint = ""
        self.submitted_prefix_tokens = 0
        self.submitted_sequence = 0
        self.last_recorded_sequence = 0
        self.accumulated_reprocessed_tokens = 0
        self.last_decision = ""
        self.submitted_at = 0.0


@dataclass
class _ScopeStats:
    observations: deque[tuple[float, int, int]] = field(
        default_factory=lambda: deque(maxlen=8)
    )
    anchor: _TrackStats = field(default_factory=_TrackStats)
    frontier: _TrackStats = field(default_factory=_TrackStats)
    anchor_base_fingerprint: str = ""
    frontier_epoch: str = ""
    next_plan_sequence: int = 0
    last_decision: str = ""
    last_access_at: float = field(default_factory=time.monotonic)
    # Request-level provider evidence, deliberately NOT attributed to a track:
    # R>0 proves a read happened somewhere, never which boundary served it.
    last_request_at: float = 0.0
    usage_observed_at: float = 0.0
    last_observed_read_at: float = 0.0
    last_observation: str = "usage_missing"
    observed_sequence: int = -1
    actual_provider: str = ""
    wire_description: dict[str, Any] | None = None
    last_round_index: int = -1
    round_attempt_count: int = 0
    stall_suspected: bool = False
    stall_rounds: int = 0
    stall_reason: str = "insufficient_observations"
    recent_cache_observations: deque[tuple[int, int, int]] = field(
        default_factory=lambda: deque(maxlen=8)
    )

    @property
    def observed_requests(self) -> int:
        return len(self.observations)

    @property
    def cache_hit_requests(self) -> int:
        return sum(cached > 0 for _, cached, _ in self.observations)

    def prune(self, *, now: float, ttl_seconds: float) -> None:
        while self.observations and now - self.observations[0][0] > ttl_seconds:
            self.observations.popleft()


@dataclass
class PromptCacheCoordinator:
    """Own provider cache policy; core only labels semantic prompt regions."""

    minimum_prefix_tokens: int = 1024
    rolling_net_threshold_tokens: int = 1024
    observation_ttl_seconds: float = 30.0 * 60.0
    max_scope_count: int = 256
    max_attempt_records: int = 128
    _handoffs: dict[str, handoff.Handoff] = field(default_factory=dict, init=False, repr=False)
    _stats: dict[str, _ScopeStats] = field(default_factory=dict, init=False, repr=False)
    _last_plan: PromptCachePlan | None = field(default=None, init=False, repr=False)
    _attempt_records: deque[dict[str, Any]] = field(
        default_factory=deque,
        init=False,
        repr=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def plan(
        self,
        request: LLMRequestIR,
        context: ShapeContext,
        encoded: EncodedRequest | None = None,
    ) -> PromptCachePlan:
        if encoded is None:
            # Compatibility for policy-only callers and tests. Runtime
            # endpoints encode once and pass the exact request explicitly.
            from pal.llm.shapes import codec_for_shape

            encoded = codec_for_shape(context.wire_shape).encode(request, context)
        profile = _resolve_profile(context)
        dialect = profile.dialect if profile else _resolve_dialect(context)
        profile_fields = {
            "profile_id": profile.profile_id if profile else "",
            "strategy": profile.strategy if profile else "",
            "profile_version": profile.version if profile else "",
            "profile_origin": str(_prompt_cache_capabilities(context.capabilities).get("origin") or ("endpoint" if profile else "default")),
            "profile_generation": str(_prompt_cache_capabilities(context.capabilities).get("generation") or "0"),
        }
        scope_key = _scope_key(request, context, dialect)
        if dialect in {PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT,
                       PromptCacheDialect.OPENAI_CHAT_EXPLICIT,
                       PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT}:
            return self._plan_handoff(request, context, encoded, dialect, profile_fields)
        if dialect == PromptCacheDialect.NONE:
            plan = PromptCachePlan(scope_key=scope_key, cache_key="", dialect=dialect)
            self._remember(plan, request=request)
            return plan
        if not _supports_explicit_breakpoints(dialect):
            with self._lock:
                stats = self._stats.setdefault(scope_key, _ScopeStats())
                stats.next_plan_sequence += 1
                sequence = stats.next_plan_sequence
            reusable_ids = {m.message_id for m in request.messages if m.prompt_region != PromptRegionIR.ACTIVE_DYNAMIC}
            plan = PromptCachePlan(
                scope_key=scope_key,
                cache_key=_cache_key(request, context),
                dialect=dialect,
                decision="provider_automatic",
                **profile_fields,
                plan_sequence=sequence,
                estimated_prefix_tokens=max((span.estimated_cache_prefix_tokens for span in encoded.message_spans if span.message_id in reusable_ids), default=0),
            )
            if profile is not None and profile.allow_stable_anchor_marker:
                spans = {span.message_id: span for span in encoded.message_spans}
                stable_span = _last_cacheable_span(
                    request,
                    spans,
                    regions={PromptRegionIR.STABLE_SYSTEM},
                )
                if (
                    stable_span is not None
                    and stable_span.estimated_cache_prefix_tokens
                    >= _capability_int(
                        context.capabilities,
                        "minimum_tokens",
                        self.minimum_prefix_tokens,
                    )
                ):
                    plan = replace(
                        plan,
                        decision="hybrid_stable_anchor",
                        allow_stable_anchor_marker=True,
                        breakpoints=(
                            PromptCacheBreakpoint(
                                label="stable_anchor",
                                message_id=stable_span.message_id,
                                ttl="30m",
                                prefix_tokens=(
                                    stable_span.estimated_cache_prefix_tokens
                                ),
                            ),
                        ),
                    )
            self._remember(plan, request=request)
            return plan

        spans = {span.message_id: span for span in encoded.message_spans}
        stable_span = _last_cacheable_span(
            request,
            spans,
            regions={PromptRegionIR.STABLE_SYSTEM},
        )
        anchor_span = _last_cacheable_span(
            request,
            spans,
            regions={PromptRegionIR.ACTIVE_INPUT},
        )
        frontier_span = _last_cacheable_span(
            request,
            spans,
            regions={PromptRegionIR.ACTIVE_HISTORY},
            require_complete=True,
        )
        estimated_prefix_tokens = max(
            anchor_span.estimated_cache_prefix_tokens if anchor_span else 0,
            frontier_span.estimated_cache_prefix_tokens if frontier_span else 0,
        )
        minimum = _capability_int(
            context.capabilities,
            "minimum_tokens",
            self.minimum_prefix_tokens,
        )
        stable_ttl = "1h" if _uses_anthropic_breakpoints(dialect) else "30m"
        anchor_ttl = stable_ttl
        frontier_ttl = "5m" if _uses_anthropic_breakpoints(dialect) else "30m"

        with self._lock:
            now = time.monotonic()
            stats = self._stats.setdefault(scope_key, _ScopeStats())
            stats.last_access_at = now
            self._prune_scopes_locked(now=now, keep=scope_key)
            stats.next_plan_sequence += 1
            plan_sequence = stats.next_plan_sequence

            if _invalidate_missing_confirmation(stats.anchor, spans):
                stats.frontier.clear()
                stats.frontier_epoch = ""
            _invalidate_missing_confirmation(stats.frontier, spans)

            stable_fingerprint = (
                stable_span.cache_prefix_fingerprint if stable_span else ""
            )
            if not stats.anchor.submitted_message_id:
                if (
                    stats.anchor_base_fingerprint
                    and stats.anchor_base_fingerprint != stable_fingerprint
                ):
                    stats.anchor.clear()
                    stats.frontier.clear()
                    stats.frontier_epoch = ""
                stats.anchor_base_fingerprint = stable_fingerprint

            frontier_epoch = (
                f"{request.metadata.get("turn_id", "")}:{anchor_span.cache_prefix_fingerprint}"
                if anchor_span is not None
                else ""
            )
            if frontier_epoch != stats.frontier_epoch:
                stats.frontier.clear()
                stats.frontier_epoch = frontier_epoch

            stable_base_tokens = (
                stable_span.estimated_cache_prefix_tokens
                if stable_span is not None
                and stable_span.estimated_cache_prefix_tokens >= minimum
                else 0
            )
            read_multiplier = _capability_float(
                context.capabilities,
                "read_multiplier",
                0.10,
            )
            write_multiplier = _capability_float(
                context.capabilities,
                "write_multiplier",
                1.25,
            )
            anchor_write_multiplier = _capability_float(
                context.capabilities,
                "anchor_write_multiplier",
                2.0 if _uses_anthropic_breakpoints(dialect) else write_multiplier,
            )
            frontier_write_multiplier = _capability_float(
                context.capabilities,
                "frontier_write_multiplier",
                write_multiplier,
            )
            net_threshold = _capability_int(
                context.capabilities,
                "net_threshold_tokens",
                self.rolling_net_threshold_tokens,
            )
            anchor_plan = _evaluate_track(
                name="anchor",
                ttl=anchor_ttl,
                epoch_key="",
                stats=stats.anchor,
                target=anchor_span,
                fallback_prefix_tokens=stable_base_tokens,
                minimum_prefix_tokens=minimum,
                read_multiplier=read_multiplier,
                write_multiplier=anchor_write_multiplier,
                net_threshold_tokens=net_threshold,
            )
            frontier_plan = _evaluate_track(
                name="frontier",
                ttl=frontier_ttl,
                epoch_key=frontier_epoch,
                stats=stats.frontier,
                target=frontier_span,
                fallback_prefix_tokens=max(
                    stable_base_tokens,
                    stats.anchor.submitted_prefix_tokens,
                ),
                minimum_prefix_tokens=minimum,
                read_multiplier=read_multiplier,
                write_multiplier=frontier_write_multiplier,
                net_threshold_tokens=net_threshold,
            )
            stats.anchor.last_decision = anchor_plan.decision
            stats.frontier.last_decision = frontier_plan.decision

        breakpoints: list[PromptCacheBreakpoint] = []
        if (
            stable_span is not None
            and stable_span.estimated_cache_prefix_tokens >= minimum
        ):
            breakpoints.append(
                _breakpoint_from_span("stable", stable_span, stable_ttl)
            )
        anchor_submitted = spans.get(anchor_plan.submitted_message_id)
        if anchor_submitted is not None:
            breakpoints.append(
                _breakpoint_from_span(
                    "anchor_submitted",
                    anchor_submitted,
                    anchor_ttl,
                )
            )
        frontier_submitted = spans.get(frontier_plan.submitted_message_id)
        if frontier_submitted is not None:
            breakpoints.append(
                _breakpoint_from_span(
                    "frontier_submitted",
                    frontier_submitted,
                    frontier_ttl,
                )
            )
        anchor_plan, breakpoints = _schedule_candidate(
            anchor_plan,
            spans,
            breakpoints,
            maximum=4,
        )
        frontier_plan, breakpoints = _schedule_candidate(
            frontier_plan,
            spans,
            breakpoints,
            maximum=4,
        )
        positions = {
            message.message_id: index
            for index, message in enumerate(request.messages)
        }
        breakpoints = _deduplicate_breakpoints(breakpoints)
        breakpoints.sort(key=lambda item: positions.get(item.message_id, -1))

        decision = f"anchor:{anchor_plan.decision};frontier:{frontier_plan.decision}"
        with self._lock:
            self._stats.setdefault(scope_key, _ScopeStats()).last_decision = decision
        plan = PromptCachePlan(
            scope_key=scope_key,
            cache_key=_cache_key(request, context),
            dialect=dialect,
            breakpoints=tuple(breakpoints),
            decision=decision,
            estimated_prefix_tokens=estimated_prefix_tokens,
            plan_sequence=plan_sequence,
            anchor=anchor_plan,
            frontier=frontier_plan,
            **profile_fields,
        )
        self._remember(plan, request=request)
        return plan

    def _plan_handoff(self, request, context, encoded, dialect, profile_fields):
        clean = handoff.clean_request(encoded)
        spans = {s.message_id: s for s in clean.message_spans}
        def boundary(span):
            if span is None or not span.cache_targets:
                return None
            return handoff.Boundary(span.message_id, span.cache_targets[-1],
                                   span.cache_prefix_fingerprint, span.estimated_cache_prefix_tokens)
        boundaries = {k: boundary(v) for k, v in spans.items() if v.cache_targets}
        stable = boundary(_last_cacheable_span(request, spans, regions={PromptRegionIR.STABLE_SYSTEM}))
        anchor = boundary(_last_cacheable_span(request, spans, regions={PromptRegionIR.ACTIVE_INPUT}))
        from pal.shared.tool_protocol import ToolResultIR
        pending_calls = set()
        frontier = None
        for message in request.messages:
            pending_calls.update(call.call_id for call in message.tool_calls)
            pending_calls.difference_update(part.call_id for part in message.parts if isinstance(part, ToolResultIR))
            if (not pending_calls and message.state == MessageState.COMPLETE
                    and message.prompt_region == PromptRegionIR.ACTIVE_HISTORY):
                frontier = boundary(spans.get(message.message_id)) or frontier
        minimum = _capability_int(context.capabilities, "minimum_tokens", self.minimum_prefix_tokens)
        if stable and stable.coordinate < minimum:
            stable = None
        # Session scope deliberately excludes the turn; upstream keys do too.
        key_request = replace(request, metadata={**request.metadata, "turn_id": "", "artifact_turn_id": ""})
        scope = _scope_key(key_request, context, dialect) + "|" + handoff.digest(
            {"policy": handoff.POLICY, "binding": _prompt_cache_capabilities(context.capabilities)})
        key = _cache_key(request, context)
        with self._lock:
            state = self._handoffs.setdefault(scope, handoff.Handoff())
            now = time.monotonic()
            state.refresh(str(request.metadata.get("turn_id") or request.metadata.get("artifact_turn_id") or ""),
                          boundaries, stable, now)
            for old_key, old_state in sorted(self._handoffs.items(), key=lambda item: item[1].last_access):
                if old_key != scope and not old_state.active and (
                    now - old_state.last_access > self.observation_ttl_seconds
                    or len(self._handoffs) > self.max_scope_count):
                    del self._handoffs[old_key]
            if not state.pending and state.cooldown is None:
                accepted = state.anchor.coordinate if state.anchor else 0
                use_anchor = anchor and anchor.coordinate >= state.max_target and anchor.coordinate > max(accepted, state.baseline)
                target, kind = (anchor, "anchor") if use_anchor else (frontier, "frontier")
                # A deferred anchor does not prevent a farther completed-round trial.
                if target and state.deferred == (target.fingerprint, state.revision):
                    target, kind = frontier, "frontier"
                state.propose(target, kind, minimum=minimum,
                    read=_capability_float(context.capabilities, "read_multiplier", .10),
                    write=_capability_float(context.capabilities, kind + "_write_multiplier",
                        _capability_float(context.capabilities, "write_multiplier", 1.25)),
                    threshold=_capability_int(context.capabilities, "net_threshold_tokens", self.rolling_net_threshold_tokens))
            points = state.markers()
            # A fourth request can continue the task using old protection only.
            candidate = state.pending
            if candidate and candidate.attempts >= 3:
                points = tuple(b for b in points if b.path != candidate.boundary.path)
            profile_fields = dict(profile_fields, strategy=handoff.POLICY)
            plan = PromptCachePlan(scope_key=scope, cache_key=key, dialect=dialect,
                breakpoints=tuple(PromptCacheBreakpoint(
                    (candidate.kind + "_candidate") if candidate and b == candidate.boundary else
                    "stable" if b == state.stable else "anchor_accepted" if b == state.anchor else "frontier_accepted",
                    b.message_id, "30m", b.coordinate) for b in points),
                decision=state.decision, plan_sequence=state.sequence + 1,
                estimated_prefix_tokens=max((b.coordinate for b in (anchor, frontier) if b), default=0),
                handoff_encoded=clean, handoff_owner=state.owner, handoff_epoch=state.economic_epoch,
                handoff_candidate=candidate.identity if candidate and candidate.attempts < 3 else "",
                **profile_fields)
            self._prune_scopes_locked(now=now, keep=scope)
            self._remember(plan, request=request)
            return plan

    def end_turn(self, turn_id: str) -> None:
        with self._lock:
            for state in self._handoffs.values():
                state.end_turn(turn_id)

    def inject(self, encoded: EncodedRequest, plan: PromptCachePlan) -> EncodedRequest:
        if plan.strategy == handoff.POLICY:
            return handoff.inject_exact(plan.handoff_encoded or encoded, plan.breakpoints, plan.cache_key,
                plan.dialect == PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
                prepared=plan.handoff_encoded is not None)
        if not plan.enabled:
            return encoded
        payload = thaw_json(encoded.payload)
        extra_body = thaw_json(encoded.extra_body)
        targets = {
            span.message_id: span.cache_targets
            for span in encoded.message_spans
        }
        applied_breakpoint_message_ids: list[str] = []

        if plan.dialect in {
            PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT,
            PromptCacheDialect.OPENAI_CHAT_EXPLICIT,
            PromptCacheDialect.OPENAI_AUTOMATIC,
            PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
        }:
            extra_body["prompt_cache_key"] = plan.cache_key
        if plan.dialect in {
            PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
            PromptCacheDialect.OPENROUTER_AUTOMATIC,
            PromptCacheDialect.OPENROUTER_ANTHROPIC_AUTOMATIC,
            PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT,
        }:
            # OpenRouter uses session_id for provider-sticky routing. Explicit
            # OpenAI caching additionally receives prompt_cache_key above.
            extra_body["session_id"] = plan.cache_key
            if plan.dialect == PromptCacheDialect.OPENROUTER_ANTHROPIC_AUTOMATIC:
                extra_body["cache_control"] = {"type": "ephemeral"}
        if (
            plan.profile_id
            and plan.dialect == PromptCacheDialect.OPENROUTER_AUTOMATIC
        ):
            # Profile-selected automatic mode keeps both stable routing keys:
            # the OpenRouter session_id above and the cache accounting key.
            extra_body["prompt_cache_key"] = plan.cache_key
        if plan.dialect in {
            PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT,
            PromptCacheDialect.OPENAI_CHAT_EXPLICIT,
            PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
        }:
            extra_body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
            for breakpoint in plan.breakpoints:
                if _mark_last_target(
                    payload,
                    targets.get(breakpoint.message_id, ()),
                    "prompt_cache_breakpoint",
                    {"mode": "explicit"},
                ):
                    applied_breakpoint_message_ids.append(breakpoint.message_id)
        elif _uses_anthropic_breakpoints(plan.dialect):
            for breakpoint in plan.breakpoints:
                marker: dict[str, Any] = {"type": "ephemeral"}
                if breakpoint.ttl in {"5m", "1h"}:
                    marker["ttl"] = breakpoint.ttl
                if _mark_last_target(
                    payload,
                    targets.get(breakpoint.message_id, ()),
                    "cache_control",
                    marker,
                ):
                    applied_breakpoint_message_ids.append(breakpoint.message_id)
        elif plan.allow_stable_anchor_marker and plan.breakpoints:
            # Hybrid: provider implicit caching stays active; the selected
            # stable anchor participates as an explicit breakpoint without
            # forcing explicit-only mode. No prompt_cache_options is sent.
            for breakpoint in plan.breakpoints:
                if _mark_last_target(
                    payload,
                    targets.get(breakpoint.message_id, ()),
                    "prompt_cache_breakpoint",
                    {"mode": "explicit"},
                ):
                    applied_breakpoint_message_ids.append(breakpoint.message_id)
        return EncodedRequest(
            payload=payload,
            message_spans=encoded.message_spans,
            extra_body=extra_body,
            applied_cache_breakpoint_message_ids=tuple(applied_breakpoint_message_ids),
        )

    def discard_accounted_attempt(self, plan: PromptCachePlan, request_id: str) -> None:
        """Release a duplicate receipt rejected by the authoritative usage ledger."""
        with self._lock:
            state = self._handoffs.get(plan.scope_key)
            if state is None:
                return
            state.active.pop(request_id, None)
            if state.pending and state.pending.attempts >= 3 and not any(
                    item.candidate == state.pending.identity for item in state.active.values()):
                state.end_candidate("candidate_budget_exhausted", cooldown=True)

    def prepare_attempt(self, request: LLMRequestIR, context: ShapeContext,
                        raw_encoded: EncodedRequest, request_id: str):
        # Keep the three-attempt reservation and actual marker snapshot atomic.
        # Concurrent callers must not both send a previously planned fourth C.
        with self._lock:
            plan = self.plan(request, context, raw_encoded)
            encoded = self.inject(raw_encoded, plan)
            diagnostics = self.start_attempt(plan, request=request, context=context,
                encoded=encoded, raw_encoded=raw_encoded, request_id=request_id)
            return plan, encoded, diagnostics

    def start_attempt(self, plan: PromptCachePlan, *, request: LLMRequestIR,
                      context: ShapeContext, encoded: EncodedRequest,
                      raw_encoded: EncodedRequest, request_id: str) -> dict[str, Any]:
        if plan.strategy == handoff.POLICY:
            with self._lock:
                state = self._handoffs[plan.scope_key]
                clean = plan.handoff_encoded or handoff.clean_request(raw_encoded)
                actual_clean = handoff.clean_request(encoded)
                actual_spans = {s.message_id: s for s in actual_clean.message_spans}
                spans = {s.message_id: s for s in clean.message_spans}
                expected = tuple(spans[b.message_id].cache_targets[-1] for b in plan.breakpoints
                                 if b.message_id in spans and spans[b.message_id].cache_targets)
                audited, wire_hash = handoff.audit(encoded, expected)
                audited &= len(expected) == len(plan.breakpoints)
                audited &= all(b.message_id in actual_spans and b.message_id in spans and
                    actual_spans[b.message_id].cache_prefix_fingerprint == spans[b.message_id].cache_prefix_fingerprint
                    for b in plan.breakpoints)
                audited &= encoded.extra_body.get("prompt_cache_key") == plan.cache_key
                if plan.dialect == PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT:
                    audited &= encoded.extra_body.get("session_id") == plan.cache_key
                audited &= plan.handoff_owner == state.owner
                candidate = state.pending
                suffix = handoff.suffix_estimate(clean, candidate.boundary.path) if candidate else None
                target_spans = [s for s in clean.message_spans if s.cache_targets and
                    any(m.message_id == s.message_id and m.prompt_region != PromptRegionIR.ACTIVE_DYNAMIC for m in request.messages)]
                target_span = max(target_spans, key=lambda s: s.estimated_cache_prefix_tokens, default=None)
                target = handoff.Boundary(target_span.message_id, target_span.cache_targets[-1],
                    target_span.cache_prefix_fingerprint, target_span.estimated_cache_prefix_tokens) if target_span else None
                state.submit(request_id, target=target, suffix=suffix,
                    round_index=int(request.metadata.get("llm_round_index") or 0),
                    audited=audited, wire_fingerprint=wire_hash, now=time.monotonic(),
                    candidate_id=plan.handoff_candidate, owner=plan.handoff_owner,
                    economic_epoch=plan.handoff_epoch,
                    points=tuple(handoff.Boundary(s.message_id, s.cache_targets[-1], s.cache_prefix_fingerprint,
                        s.estimated_cache_prefix_tokens) for b in plan.breakpoints
                        if (s := spans.get(b.message_id)) and s.cache_targets))
                if not audited:
                    state.decision = "marker_set_uncontrolled"
        description = describe_request(request, raw_encoded, encoded)
        now = time.monotonic()
        with self._lock:
            stats = self._stats.setdefault(plan.scope_key, _ScopeStats())
            changes = compare_requests(stats.wire_description, description)
            gap = now - stats.last_request_at if stats.last_request_at else None
            round_index = int(request.metadata.get("llm_round_index") or 0)
            stats.round_attempt_count = stats.round_attempt_count + 1 if stats.last_round_index == round_index else 1
            stats.last_round_index = round_index
            stats.wire_description = description
            stats.last_request_at = now
            stats.last_access_at = now
            if plan.strategy == handoff.POLICY:
                stats.next_plan_sequence = plan.plan_sequence
            for track, track_plan in (() if plan.strategy == handoff.POLICY else ((stats.anchor, plan.anchor), (stats.frontier, plan.frontier))):
                _record_track_success(track, track_plan, plan_sequence=plan.plan_sequence,
                    applied_breakpoint_ids=frozenset(encoded.applied_cache_breakpoint_message_ids), observed_at=now)
            self._prune_scopes_locked(now=now, keep=plan.scope_key)
            diagnostics = {
                **{k: v for k, v in description.items() if not k.startswith("_")}, **changes,
                "task_id": str(request.metadata.get("task_id") or ""),
                "turn_id": str(request.metadata.get("turn_id") or request.metadata.get("artifact_turn_id") or ""),
                "round_index": round_index, "attempt_index": stats.round_attempt_count,
                "previous_request_gap": gap, "started_at": time.time(),
                "retry_recovery_cause": str(request.metadata.get("max_output_recovery_stage") or request.metadata.get("retry_cause") or ""),
                "session_key_hash": _short_hash(str(encoded.extra_body.get("session_id") or "")),
            }
        if plan.strategy == handoff.POLICY:
            diagnostics.update(cache_handoff=state.snapshot(), wire_audit_fingerprint=wire_hash)
        self.record_attempt(plan, status="submitted", request_id=request_id, **diagnostics)
        return diagnostics

    def record_attempt(
        self,
        plan: PromptCachePlan,
        *,
        status: str,
        request_id: str = "",
        endpoint_id: str = "",
        model_id: str = "",
        provider_id: str = "",
        wire_shape: str = "",
        finish_reason: str = "",
        error: str = "",
        elapsed_seconds: float = 0.0,
        provider_generation_id: str = "",
        usage: LLMUsageIR | None = None,
        applied_cache_breakpoint_message_ids: tuple[str, ...] = (),
        **diagnostics: Any,
    ) -> None:
        """Append one bounded, hash-only attempt record for cache diagnosis.

        Diagnostic evidence only: this never changes planning, confirmation,
        or retry semantics. Raw scope/cache keys never enter records.
        """

        if plan.strategy == handoff.POLICY and status != "submitted":
            with self._lock:
                state = self._handoffs.get(plan.scope_key)
                if state is not None:
                    state.settle(request_id, usage or LLMUsageIR(), success=status == "success",
                        now=time.monotonic(), actual_provider=str(diagnostics.get("actual_provider") or ""),
                        returned_model=str(diagnostics.get("returned_model") or ""),
                        service_tier=str(diagnostics.get("service_tier") or ""),
                        normal_round=finish_reason not in {"length", "error"})
                    diagnostics["cache_handoff"] = state.snapshot()
        applied = tuple(str(item) for item in applied_cache_breakpoint_message_ids)
        usage_reported = usage is not None and usage.reported
        cached = max(0, int(usage.cached_input_tokens)) if usage is not None else 0
        write = max(0, int(usage.cache_write_input_tokens)) if usage is not None else 0
        explicit = _supports_explicit_breakpoints(plan.dialect)
        if usage is None or not usage.reported:
            observation = "usage_missing"
        elif cached > 0:
            observation = "read_observed"
        elif write > 0:
            observation = "write_observed"
        elif usage.has("cached_input_tokens") and usage.has("cache_write_input_tokens"):
            observation = "reported_zero"
        else:
            observation = "usage_missing"
        record: dict[str, Any] = {
            **diagnostics,
            "attempt_id": str(request_id or ""),
            "provider_generation_id": str(provider_generation_id or ""),
            "status": str(status or "unknown"),
            "scope_key_hash": _short_hash(plan.scope_key),
            "cache_key_hash": _short_hash(plan.cache_key),
            "dialect": plan.dialect.value,
            "cache_mode": "explicit" if explicit else "automatic",
            "profile_id": str(plan.profile_id or ""),
            "profile_version": plan.profile_version,
            "profile_origin": plan.profile_origin,
            "profile_generation": plan.profile_generation,
            "strategy": str(plan.strategy or ""),
            "endpoint_id": str(endpoint_id or ""),
            "model_id": str(model_id or ""),
            "provider_id": str(provider_id or ""),
            "wire_shape": str(wire_shape or ""),
            "plan_decision": str(plan.decision or ""),
            "plan_sequence": int(plan.plan_sequence),
            "estimated_prefix_tokens": int(plan.estimated_prefix_tokens),
            "planned_markers": [
                {
                    "label": item.label,
                    "message_id": item.message_id,
                    "ttl": item.ttl,
                    "prefix_tokens": int(item.prefix_tokens),
                }
                for item in plan.breakpoints
            ],
            "applied_marker_ids": list(applied),
            "anchor_decision": str(plan.anchor.decision or ""),
            "frontier_decision": str(plan.frontier.decision or ""),
            "anchor_candidate_message_id": str(plan.anchor.candidate_message_id or ""),
            "frontier_candidate_message_id": str(
                plan.frontier.candidate_message_id or ""
            ),
            "finish_reason": str(finish_reason or ""),
            "error": str(error or "")[:200],
            "elapsed_seconds": round(float(elapsed_seconds or 0.0), 6),
            "usage_reported": bool(usage_reported),
            "reported_fields": list(usage.reported_fields) if usage else [],
            "raw_usage_counters": dict(usage.raw_counters) if usage else {},
            "input_accounting": usage.input_accounting if usage else "unknown",
            "usage_final": bool(usage and usage.final),
            "actual_cost": usage.cost if usage and usage.cost_reported else None,
            "cost_status": "reported" if usage and usage.cost_reported else "unknown",
            "output_tokens": usage.output_tokens if usage else 0,
            "reasoning_tokens": usage.reasoning_tokens if usage else 0,
            "input_tokens": max(0, int(usage.input_tokens)) if usage is not None else 0,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": write,
            "uncached_input_tokens": (
                max(0, int(usage.uncached_input_tokens)) if usage is not None else 0
            ),
            "read_observed": bool(usage and usage.has("cached_input_tokens") and cached > 0),
            "write_observed": bool(usage and usage.has("cache_write_input_tokens") and write > 0),
            "observation": observation,
            "usage_anomaly": (
                str(getattr(usage, "usage_anomaly", "") or "")
                if usage is not None
                else ""
            ),
            "usage_invariant_violation": bool(
                usage_reported
                and usage is not None
                and usage.cache_partition_reported
                and usage.input_tokens
                != usage.uncached_input_tokens + cached + write
            ),
            # Zero read+write with accepted-looking explicit markers is the
            # stall signature this record exists to expose. Evidence only;
            # confirmation semantics are unchanged.
            "zero_read_write_with_applied_markers": bool(
                applied and observation == "reported_zero"
            ),
            "cache_handoff": diagnostics.get("cache_handoff"),
            "wire_audit_fingerprint": diagnostics.get("wire_audit_fingerprint", ""),
            "prompt_cache_diagnostics": diagnostics.get("prompt_cache_diagnostics"),
            "recorded_at": time.time(),
        }
        with self._lock:
            previous = next((item for item in self._attempt_records if request_id and item["attempt_id"] == request_id), None)
            if previous is not None:
                if previous["status"] != "submitted":
                    return
                previous.update(record)
            else:
                self._attempt_records.append(record)
            if status in {"failed", "cancelled"}:
                stats = self._stats.get(plan.scope_key)
                if stats is not None and plan.plan_sequence >= stats.observed_sequence:
                    stats.observed_sequence = plan.plan_sequence
                    stats.stall_suspected = False
                    stats.stall_rounds = 0
                    stats.stall_reason = "attempt_failed"
                    stats.recent_cache_observations.clear()
            while len(self._attempt_records) > max(1, int(self.max_attempt_records)):
                self._attempt_records.popleft()

    def record_attempt_failure(
        self,
        plan: PromptCachePlan,
        *,
        request_id: str = "",
        endpoint_id: str = "",
        model_id: str = "",
        provider_id: str = "",
        wire_shape: str = "",
        finish_reason: str = "",
        error: str = "",
        elapsed_seconds: float = 0.0,
        provider_generation_id: str = "",
    ) -> None:
        self.record_attempt(
            plan,
            status="failed",
            request_id=request_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            provider_id=provider_id,
            wire_shape=wire_shape,
            finish_reason=finish_reason,
            error=error,
            elapsed_seconds=elapsed_seconds,
            provider_generation_id=provider_generation_id,
        )

    def record_success(
        self,
        plan: PromptCachePlan,
        usage: LLMUsageIR,
        *,
        applied_cache_breakpoint_message_ids: tuple[str, ...] = (),
        request_id: str = "",
        endpoint_id: str = "",
        model_id: str = "",
        provider_id: str = "",
        wire_shape: str = "",
        finish_reason: str = "",
        elapsed_seconds: float = 0.0,
        provider_generation_id: str = "",
        **diagnostics: Any,
    ) -> None:
        applied_breakpoint_ids = frozenset(applied_cache_breakpoint_message_ids)
        with self._lock:
            stats = self._stats.setdefault(plan.scope_key, _ScopeStats())
            now = time.monotonic()
            stats.last_access_at = now
            self._prune_scopes_locked(now=now, keep=plan.scope_key)
            if plan.plan_sequence == stats.next_plan_sequence and plan.plan_sequence > stats.observed_sequence:
                if usage.has("cached_input_tokens"):
                    stats.observations.append(
                        (
                            now,
                            max(0, int(usage.cached_input_tokens)),
                            max(0, int(usage.cache_write_input_tokens)),
                        )
                    )
                _record_track_success(
                    stats.anchor,
                    plan.anchor,
                    plan_sequence=plan.plan_sequence,
                    applied_breakpoint_ids=applied_breakpoint_ids,
                    observed_at=now,
                )
                if plan.frontier.epoch_key == stats.frontier_epoch:
                    _record_track_success(
                        stats.frontier,
                        plan.frontier,
                        plan_sequence=plan.plan_sequence,
                        applied_breakpoint_ids=applied_breakpoint_ids,
                        observed_at=now,
                    )
                _record_scope_observation(
                    stats,
                    usage,
                    estimated_prefix_tokens=plan.estimated_prefix_tokens,
                    scope_hash=_short_hash(plan.scope_key),
                    observed_at=now,
                    sequence=plan.plan_sequence,
                    actual_provider=str(diagnostics.get("actual_provider") or ""),
                    comparable=bool(diagnostics.get("prefix_preserved") or diagnostics.get("change_reason") == "first_request"),
                    dynamic_unchanged=bool(diagnostics.get("dynamic_unchanged", True)),
                    previous_gap=diagnostics.get("previous_request_gap"),
                )
        self.record_attempt(
            plan,
            status="success",
            request_id=request_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            provider_id=provider_id,
            wire_shape=wire_shape,
            finish_reason=finish_reason,
            elapsed_seconds=elapsed_seconds,
            provider_generation_id=provider_generation_id,
            usage=usage,
            applied_cache_breakpoint_message_ids=tuple(applied_breakpoint_ids),
            **diagnostics,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            plan = self._last_plan
            now = time.monotonic()
            self._prune_scopes_locked(now=now)
            observed = sum(item.observed_requests for item in self._stats.values())
            hit_requests = sum(item.cache_hit_requests for item in self._stats.values())
            last_stats = (
                self._stats.get(plan.scope_key)
                if plan is not None
                else None
            )
            return {
                "handoff": self._handoffs[plan.scope_key].snapshot() if plan and plan.scope_key in self._handoffs else None,
                "dialect": plan.dialect.value if plan is not None else "none",
                "decision": plan.decision if plan is not None else "not_planned",
                "estimated_prefix_tokens": (
                    plan.estimated_prefix_tokens if plan is not None else 0
                ),
                "breakpoints": (
                    [item.label for item in plan.breakpoints] if plan is not None else []
                ),
                "confirmed_checkpoint": False,
                "confirmed_prefix_tokens": 0,
                "submitted_checkpoint": bool(
                    last_stats
                    and (
                        last_stats.anchor.submitted_message_id
                        or last_stats.frontier.submitted_message_id
                    )
                ),
                "candidate_checkpoint": bool(
                    plan and plan.candidate_message_id
                ),
                "submitted_prefix_tokens": (
                    max(
                        last_stats.anchor.submitted_prefix_tokens,
                        last_stats.frontier.submitted_prefix_tokens,
                    )
                    if last_stats
                    else 0
                ),
                "candidate_delta_tokens": (
                    plan.reprocessed_delta_tokens if plan is not None else 0
                ),
                "accumulated_reprocessed_tokens": (
                    max(
                        last_stats.anchor.accumulated_reprocessed_tokens,
                        last_stats.frontier.accumulated_reprocessed_tokens,
                    )
                    if last_stats
                    else 0
                ),
                "projected_reprocessed_tokens": (
                    plan.projected_reprocessed_tokens if plan is not None else 0
                ),
                "estimated_net_tokens": (
                    plan.estimated_net_tokens if plan is not None else 0.0
                ),
                "observed_request_count": observed,
                "request_hit_rate": hit_requests / observed if observed else 0.0,
                "scope_count": len(set(self._stats) | set(self._handoffs)),
                "last_scope_key": plan.scope_key if plan is not None else "",
                "attempt_record_count": len(self._attempt_records),
                "recent_attempts": [
                    thaw_json(item) for item in list(self._attempt_records)[-20:]
                ],
                "observation": {
                    "state": (
                        last_stats.last_observation if last_stats else "usage_missing"
                    ),
                    "usage_observed_at": (
                        last_stats.usage_observed_at if last_stats else 0.0
                    ),
                    "last_observed_read_at": (
                        last_stats.last_observed_read_at if last_stats else 0.0
                    ),
                    "last_observed_sequence": last_stats.observed_sequence if last_stats else -1,
                    "last_request_at": (
                        last_stats.last_request_at if last_stats else 0.0
                    ),
                    "stall_suspected": (
                        bool(last_stats.stall_suspected) if last_stats else False
                    ),
                    "stall_rounds": last_stats.stall_rounds if last_stats else 0,
                    "stall_reason": last_stats.stall_reason if last_stats else "no_requests",
                },
                "anchor": _track_snapshot(
                    plan.anchor if plan is not None else None,
                    last_stats.anchor if last_stats else None,
                ),
                "frontier": _track_snapshot(
                    plan.frontier if plan is not None else None,
                    last_stats.frontier if last_stats else None,
                ),
            }

    def warm_deadline_snapshot(self, *, logical_scope_id: str = "", endpoint_id: str = "") -> dict[str, Any]:
        return {"eligible": False, "reason": "boundary_evidence_unavailable",
                "anchor_epoch": "", "anchor_ttl_seconds": 0, "prefix_tokens": 0}

    def confirmed_anchor_request(self, *, logical_scope_id: str = "", endpoint_id: str = "") -> dict[str, Any]:
        # Submitted marker bytes remain in the planner, but must not authorize
        # a cache-dependent compaction request or an expiry assertion.
        return {}

    def _remember(
        self,
        plan: PromptCachePlan,
        *,
        request: LLMRequestIR,
    ) -> None:
        with self._lock:
            self._last_plan = plan

    def _prune_scopes_locked(self, *, now: float, keep: str = "") -> None:
        for scope_key, stats in tuple(self._stats.items()):
            stats.prune(now=now, ttl_seconds=self.observation_ttl_seconds)
            if (
                scope_key != keep
                and not stats.observations
                and now - stats.last_access_at > self.observation_ttl_seconds
            ):
                self._stats.pop(scope_key, None)
        maximum = max(1, int(self.max_scope_count))
        if len(self._stats) <= maximum:
            return
        candidates = sorted(
            (
                (stats.last_access_at, scope_key)
                for scope_key, stats in self._stats.items()
                if scope_key != keep
            )
        )
        for _, scope_key in candidates[: max(0, len(self._stats) - maximum)]:
            self._stats.pop(scope_key, None)


def _last_cacheable_span(
    request: LLMRequestIR,
    spans: Mapping[str, EncodedMessageSpan],
    *,
    regions: set[PromptRegionIR],
    require_complete: bool = False,
) -> EncodedMessageSpan | None:
    for message in reversed(request.messages):
        if message.prompt_region not in regions:
            continue
        if require_complete and message.state != MessageState.COMPLETE:
            continue
        span = spans.get(message.message_id)
        if (
            span is not None
            and span.cache_targets
            and span.cache_prefix_fingerprint
            and span.estimated_cache_prefix_tokens > 0
        ):
            return span
    return None


def _invalidate_missing_confirmation(
    stats: _TrackStats,
    spans: Mapping[str, EncodedMessageSpan],
) -> bool:
    if not stats.submitted_message_id:
        return False
    span = spans.get(stats.submitted_message_id)
    if span is None:
        span = next((item for item in spans.values() if item.cache_prefix_fingerprint == stats.submitted_fingerprint), None)
        if span is not None:
            stats.submitted_message_id = span.message_id
    if (
        span is None
        or not span.cache_prefix_fingerprint
        or span.cache_prefix_fingerprint != stats.submitted_fingerprint
    ):
        stats.clear()
        return True
    stats.submitted_prefix_tokens = span.estimated_cache_prefix_tokens
    return False


def _evaluate_track(
    *,
    name: str,
    ttl: str,
    epoch_key: str,
    stats: _TrackStats,
    target: EncodedMessageSpan | None,
    fallback_prefix_tokens: int,
    minimum_prefix_tokens: int,
    read_multiplier: float,
    write_multiplier: float,
    net_threshold_tokens: int,
) -> PromptCacheTrackPlan:
    common = {
        "name": name,
        "ttl": ttl,
        "epoch_key": epoch_key,
        "submitted_message_id": stats.submitted_message_id,
        "submitted_fingerprint": stats.submitted_fingerprint,
        "submitted_prefix_tokens": stats.submitted_prefix_tokens,
    }
    if target is None:
        return PromptCacheTrackPlan(**common)
    target_tokens = target.estimated_cache_prefix_tokens
    target_fields = {
        **common,
        "target_message_id": target.message_id,
        "target_fingerprint": target.cache_prefix_fingerprint,
        "target_prefix_tokens": target_tokens,
    }
    if (
        stats.submitted_message_id == target.message_id
        and stats.submitted_fingerprint == target.cache_prefix_fingerprint
    ):
        return PromptCacheTrackPlan(
            **target_fields,
            decision="submitted_reuse",
            planning_base_tokens=target_tokens,
        )

    # This is the baseline for deciding how much NEW input warrants another
    # breakpoint, not evidence of a provider cache hit or a billing discount.
    # The caller validates submitted fingerprints before passing these spans.
    base_tokens = min(target_tokens, max(0, stats.submitted_prefix_tokens, fallback_prefix_tokens))
    delta = max(0, target_tokens - base_tokens)
    projected = stats.accumulated_reprocessed_tokens + delta
    estimated_net = (
        projected * max(0.0, 1.0 - read_multiplier)
        - delta * max(0.0, write_multiplier - 1.0)
    )
    if target_tokens < minimum_prefix_tokens:
        decision = "below_provider_minimum"
        candidate = ""
    elif estimated_net >= net_threshold_tokens:
        decision = "economic_advance"
        candidate = target.message_id
    else:
        decision = "economic_batching"
        candidate = ""
    return PromptCacheTrackPlan(
        **target_fields,
        decision=decision,
        candidate_message_id=candidate,
        planning_base_tokens=base_tokens,
        reprocessed_delta_tokens=delta,
        projected_reprocessed_tokens=projected,
        estimated_net_tokens=estimated_net,
    )


def _breakpoint_from_span(
    label: str,
    span: EncodedMessageSpan,
    ttl: str,
) -> PromptCacheBreakpoint:
    return PromptCacheBreakpoint(
        label=label,
        message_id=span.message_id,
        ttl=ttl,
        prefix_tokens=span.estimated_cache_prefix_tokens,
    )


def _schedule_candidate(
    track: PromptCacheTrackPlan,
    spans: Mapping[str, EncodedMessageSpan],
    breakpoints: list[PromptCacheBreakpoint],
    *,
    maximum: int,
) -> tuple[PromptCacheTrackPlan, list[PromptCacheBreakpoint]]:
    if not track.candidate_message_id:
        return track, breakpoints
    span = spans.get(track.candidate_message_id)
    if span is None:
        return replace(
            track,
            candidate_message_id="",
            decision="target_unavailable",
        ), breakpoints
    if len(_deduplicate_breakpoints(breakpoints)) >= maximum:
        return replace(
            track,
            candidate_message_id="",
            decision="slot_deferred",
        ), breakpoints
    return track, [
        *breakpoints,
        _breakpoint_from_span(f"{track.name}_candidate", span, track.ttl),
    ]


def _deduplicate_breakpoints(
    breakpoints: list[PromptCacheBreakpoint],
) -> list[PromptCacheBreakpoint]:
    deduplicated: list[PromptCacheBreakpoint] = []
    seen: set[str] = set()
    for breakpoint in breakpoints:
        if breakpoint.message_id in seen:
            continue
        seen.add(breakpoint.message_id)
        deduplicated.append(breakpoint)
    return deduplicated


def _short_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _record_scope_observation(
    stats: _ScopeStats, usage: LLMUsageIR, *, estimated_prefix_tokens: int,
    scope_hash: str, observed_at: float, sequence: int, actual_provider: str,
    comparable: bool, dynamic_unchanged: bool, previous_gap: float | None,
) -> None:
    if sequence <= stats.observed_sequence:
        return
    stats.observed_sequence = sequence
    cached, write = usage.cached_input_tokens, usage.cache_write_input_tokens
    complete = usage.has("cached_input_tokens") and usage.has("cache_write_input_tokens")
    stats.last_observation = ("read_observed" if usage.has("cached_input_tokens") and cached > 0 else
                             "write_observed" if usage.has("cache_write_input_tokens") and write > 0 else
                             "reported_zero" if complete else "usage_missing")
    if stats.last_observation != "usage_missing":
        stats.usage_observed_at = observed_at
    if usage.has("cached_input_tokens") and cached > 0:
        stats.last_observed_read_at = observed_at
    same_provider = bool(actual_provider) and (not stats.actual_provider or actual_provider == stats.actual_provider)
    stats.actual_provider = actual_provider
    stats.stall_reason = (
        "cache_fields_missing" if not complete else "usage_anomaly" if usage.usage_anomaly else
        "provider_unknown" if not actual_provider else "provider_changed" if not same_provider else
        "prefix_changed_or_unknown" if not comparable else "dynamic_changed" if not dynamic_unchanged else
        "long_gap" if previous_gap is not None and previous_gap > 300 else ""
    )
    if stats.stall_reason:
        stats.recent_cache_observations.clear()
        stats.stall_suspected = False
        stats.stall_rounds = 0
        return
    stats.recent_cache_observations.append((int(estimated_prefix_tokens), cached, write))
    stalled, rounds = _detect_frontier_stall(stats.recent_cache_observations)
    if stalled and not stats.stall_suspected:
        logger.warning("cache_frontier_stalled_suspected (scope_hash=%s, rounds=%d)", scope_hash, rounds)
    stats.stall_suspected = stalled
    stats.stall_rounds = rounds if stalled else 0
    stats.stall_reason = "suspected" if stalled else "insufficient_stall_evidence"


def _detect_frontier_stall(
    recent: "deque[tuple[int, int, int]]",
) -> tuple[bool, int]:
    """Diagnostic only: three consecutive reported rounds where the reusable
    prefix clearly grows while observed reads stay flat and writes stay zero.
    Never triggers side effects; missing usage never counts as evidence."""

    window = list(recent)[-3:]
    if len(window) < 3:
        return False, 0
    estimated = [item[0] for item in window]
    cached = [item[1] for item in window]
    write = [item[2] for item in window]
    grew = all(
        estimated[index + 1] - estimated[index] >= 1024
        for index in range(len(estimated) - 1)
    )
    flat_reads = max(cached) - min(cached) <= 1024
    zero_write = all(item == 0 for item in write)
    stalled = bool(grew and flat_reads and zero_write)
    return stalled, 3 if stalled else 0


def _record_track_success(
    stats: _TrackStats,
    plan: PromptCacheTrackPlan,
    *,
    plan_sequence: int,
    applied_breakpoint_ids: frozenset[str],
    observed_at: float,
) -> None:
    if plan_sequence <= stats.last_recorded_sequence:
        return
    stats.last_recorded_sequence = plan_sequence
    if (
        plan.candidate_message_id
        and plan.candidate_message_id in applied_breakpoint_ids
    ):
        if stats.submitted_message_id != plan.candidate_message_id:
            stats.submitted_at = observed_at
        stats.submitted_message_id = plan.candidate_message_id
        stats.submitted_fingerprint = plan.target_fingerprint
        stats.submitted_prefix_tokens = plan.target_prefix_tokens
        stats.submitted_sequence = plan_sequence
        stats.accumulated_reprocessed_tokens = 0
        return
    if plan.reprocessed_delta_tokens <= 0:
        return
    if (
        plan.submitted_message_id != stats.submitted_message_id
        or plan.submitted_fingerprint != stats.submitted_fingerprint
    ):
        return
    stats.accumulated_reprocessed_tokens += plan.reprocessed_delta_tokens


def _track_snapshot(
    plan: PromptCacheTrackPlan | None,
    stats: _TrackStats | None,
) -> dict[str, Any]:
    return {
        "decision": plan.decision if plan is not None else "not_planned",
        "ttl": plan.ttl if plan is not None else "",
        "submitted_at": stats.submitted_at if stats else 0.0,
        "confirmed": False,
        "confirmed_prefix_tokens": 0,
        "submitted": bool(stats and stats.submitted_message_id),
        "candidate": bool(plan and plan.candidate_message_id),
        "submitted_prefix_tokens": stats.submitted_prefix_tokens if stats else 0,
        "target_prefix_tokens": plan.target_prefix_tokens if plan else 0,
        "economics_assumption": "incremental_reuse_estimate",
        "planning_base_tokens": plan.planning_base_tokens if plan else 0,
        "candidate_delta_tokens": plan.reprocessed_delta_tokens if plan else 0,
        "accumulated_reprocessed_tokens": (
            stats.accumulated_reprocessed_tokens if stats else 0
        ),
        "projected_reprocessed_tokens": (
            plan.projected_reprocessed_tokens if plan else 0
        ),
        "estimated_net_tokens": plan.estimated_net_tokens if plan else 0.0,
    }


class CacheProfileError(ValueError):
    """Invalid trusted cache configuration; reject before provider invocation."""


def _resolve_profile(context: ShapeContext) -> CacheProfile | None:
    override = _prompt_cache_capabilities(context.capabilities)
    if override.get("enabled") is False:
        return None
    name = str(override.get("cache_profile") or "").strip()
    if not name:
        return None
    profile = CACHE_PROFILES.get(name)
    if profile is None:
        raise CacheProfileError(f"unknown cache_profile: {name}")
    if name == handoff.POLICY:
        dialect = _resolve_dialect(context)
        if dialect not in {PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT,
                           PromptCacheDialect.OPENAI_CHAT_EXPLICIT,
                           PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT}:
            raise CacheProfileError("economic explicit policy requires a supported OpenAI explicit endpoint")
        return replace(profile, dialect=dialect)
    if (context.provider_id.strip().lower() != "openrouter"
            or context.model_id != "openai/gpt-6-astra"
            or context.wire_shape != WireShape.OPENAI_RESPONSE):
        raise CacheProfileError(f"cache_profile {name} requires OpenRouter openai/gpt-6-astra Responses")
    requested = override.get("dialect")
    if requested and requested != profile.dialect.value:
        raise CacheProfileError("cache_profile conflicts with explicit dialect")
    return profile


def validate_cache_policy(context: ShapeContext) -> None:
    _resolve_profile(context)
    _resolve_dialect(context)


def _resolve_dialect(context: ShapeContext) -> PromptCacheDialect:
    override = _prompt_cache_capabilities(context.capabilities)
    if override.get("enabled") is False:
        return PromptCacheDialect.NONE
    requested = str(override.get("dialect") or "").strip().lower()
    if requested:
        try:
            return PromptCacheDialect(requested)
        except ValueError as exc:
            raise CacheProfileError(f"unknown cache dialect: {requested}") from exc

    provider = str(context.provider_id or "").strip().lower()
    base_url = str(context.base_url or "").strip().lower()
    is_openrouter = "openrouter" in provider or "openrouter.ai" in base_url
    is_anthropic_model = "anthropic" in str(context.model_id or "").lower()
    if is_openrouter:
        if context.wire_shape == WireShape.ANTHROPIC_MESSAGES:
            return PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT
        if is_anthropic_model:
            return PromptCacheDialect.OPENROUTER_ANTHROPIC_AUTOMATIC
        if (
            context.wire_shape
            in {WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION}
            and _supports_openai_explicit(context.model_id)
        ):
            return PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT
        return PromptCacheDialect.OPENROUTER_AUTOMATIC
    if context.wire_shape == WireShape.ANTHROPIC_MESSAGES and "anthropic" in provider:
        return PromptCacheDialect.ANTHROPIC_EXPLICIT
    if "openai" in provider and _supports_openai_explicit(context.model_id):
        if context.wire_shape == WireShape.OPENAI_RESPONSE:
            return PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT
        if context.wire_shape == WireShape.OPENAI_COMPLETION:
            return PromptCacheDialect.OPENAI_CHAT_EXPLICIT
    if "openai" in provider and context.wire_shape in {
        WireShape.OPENAI_RESPONSE,
        WireShape.OPENAI_COMPLETION,
    }:
        return PromptCacheDialect.OPENAI_AUTOMATIC
    return PromptCacheDialect.NONE


def _supports_explicit_breakpoints(dialect: PromptCacheDialect) -> bool:
    return dialect in {
        PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT,
        PromptCacheDialect.OPENAI_CHAT_EXPLICIT,
        PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
        PromptCacheDialect.ANTHROPIC_EXPLICIT,
        PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT,
    }


def _uses_anthropic_breakpoints(dialect: PromptCacheDialect) -> bool:
    return dialect in {
        PromptCacheDialect.ANTHROPIC_EXPLICIT,
        PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT,
    }


def _supports_openai_explicit(model_id: str) -> bool:
    normalized = str(model_id or "").strip().lower()
    match = re.search(
        r"(?:^|/)gpt-(\d+)(?:\.(\d+))?(?:-|$)",
        normalized,
    )
    if match is None:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major > 5 or (major == 5 and minor >= 6)


def _scope_key(
    request: LLMRequestIR,
    context: ShapeContext,
    dialect: PromptCacheDialect,
) -> str:
    return "|".join(
        (
            request.logical_scope_id or "default",
            context.endpoint_id,
            context.model_id,
            context.wire_shape.value,
            dialect.value,
            _short_hash(context.provider_id + "|" + context.base_url),
            str(_prompt_cache_capabilities(context.capabilities).get("cache_profile") or "legacy"),
            str(_prompt_cache_capabilities(context.capabilities).get("generation") or "0"),
            str(request.metadata.get("turn_id") or request.metadata.get("artifact_turn_id") or ""),
        )
    )


def _cache_key(request: LLMRequestIR, context: ShapeContext) -> str:
    material = json.dumps(
        {
            "scope": request.logical_scope_id or "default",
            "endpoint": context.endpoint_id,
            "model": context.model_id,
            "shape": context.wire_shape.value,
            **({"cache_profile": _prompt_cache_capabilities(context.capabilities)["cache_profile"]}
               if _prompt_cache_capabilities(context.capabilities).get("cache_profile") else {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "pal-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]


def _mark_last_target(
    payload: dict[str, Any],
    paths: tuple[tuple[str | int, ...], ...],
    key: str,
    value: Mapping[str, Any],
) -> bool:
    for path in reversed(paths):
        target: Any = payload
        try:
            for part in path:
                target = target[part]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(target, dict):
            if key == "prompt_cache_breakpoint" and "content" in target:
                content = target.get("content")
                if isinstance(content, str) and content:
                    target["content"] = [
                        {"type": "text", "text": content, key: dict(value)}
                    ]
                    return True
                if isinstance(content, list):
                    for block in reversed(content):
                        if isinstance(block, dict):
                            block[key] = dict(value)
                            return True
            target[key] = dict(value)
            return True
    return False


def _prompt_cache_capabilities(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    value = capabilities.get("prompt_cache") if isinstance(capabilities, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _capability_int(
    capabilities: Mapping[str, Any],
    name: str,
    default: int,
) -> int:
    value = _prompt_cache_capabilities(capabilities).get(name)
    try:
        return max(0, int(value)) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _capability_float(
    capabilities: Mapping[str, Any],
    name: str,
    default: float,
) -> float:
    value = _prompt_cache_capabilities(capabilities).get(name)
    try:
        return max(0.0, float(value)) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)
