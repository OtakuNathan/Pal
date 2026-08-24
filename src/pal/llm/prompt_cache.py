from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock
from typing import Any, Mapping

from pal.llm.ir import (
    LLMRequestIR,
    LLMUsageIR,
    MessageState,
    PromptRegionIR,
    WireShape,
)
from pal.llm.shapes.base import EncodedMessageSpan, EncodedRequest, ShapeContext
from pal.shared.json_values import thaw_json


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
    confirmed_message_id: str = ""
    confirmed_fingerprint: str = ""
    confirmed_prefix_tokens: int = 0
    target_message_id: str = ""
    target_fingerprint: str = ""
    target_prefix_tokens: int = 0
    candidate_message_id: str = ""
    reprocessed_delta_tokens: int = 0
    projected_reprocessed_tokens: int = 0
    estimated_net_tokens: float = 0.0


@dataclass(frozen=True)
class PromptCachePlan:
    scope_key: str
    cache_key: str
    dialect: PromptCacheDialect
    breakpoints: tuple[PromptCacheBreakpoint, ...] = ()
    decision: str = "disabled"
    estimated_prefix_tokens: int = 0
    plan_sequence: int = 0
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
    def confirmed_message_id(self) -> str:
        return self.frontier.confirmed_message_id or self.anchor.confirmed_message_id

    @property
    def candidate_message_id(self) -> str:
        return self.anchor.candidate_message_id or self.frontier.candidate_message_id

    @property
    def confirmed_prefix_tokens(self) -> int:
        return max(
            self.anchor.confirmed_prefix_tokens,
            self.frontier.confirmed_prefix_tokens,
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


@dataclass
class _TrackStats:
    confirmed_message_id: str = ""
    confirmed_fingerprint: str = ""
    confirmed_prefix_tokens: int = 0
    confirmed_sequence: int = 0
    last_used_at: float = 0.0
    accumulated_reprocessed_tokens: int = 0
    last_decision: str = ""

    def clear(self) -> None:
        self.confirmed_message_id = ""
        self.confirmed_fingerprint = ""
        self.confirmed_prefix_tokens = 0
        self.confirmed_sequence = 0
        self.last_used_at = 0.0
        self.accumulated_reprocessed_tokens = 0
        self.last_decision = ""


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
    _stats: dict[str, _ScopeStats] = field(default_factory=dict, init=False, repr=False)
    _last_plan: PromptCachePlan | None = field(default=None, init=False, repr=False)
    _last_plans_by_scope: dict[str, PromptCachePlan] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _last_requests_by_scope: dict[str, LLMRequestIR] = field(
        default_factory=dict,
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
        dialect = _resolve_dialect(context)
        scope_key = _scope_key(request, context, dialect)
        if dialect == PromptCacheDialect.NONE:
            plan = PromptCachePlan(scope_key=scope_key, cache_key="", dialect=dialect)
            self._remember(plan, request=request)
            return plan
        if not _supports_explicit_breakpoints(dialect):
            plan = PromptCachePlan(
                scope_key=scope_key,
                cache_key=_cache_key(request, context),
                dialect=dialect,
                decision="provider_automatic",
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
            if not stats.anchor.confirmed_message_id:
                if (
                    stats.anchor_base_fingerprint
                    and stats.anchor_base_fingerprint != stable_fingerprint
                ):
                    stats.anchor.clear()
                    stats.frontier.clear()
                    stats.frontier_epoch = ""
                stats.anchor_base_fingerprint = stable_fingerprint

            frontier_epoch = (
                f"{anchor_span.message_id}:{anchor_span.cache_prefix_fingerprint}"
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
                    stats.anchor.confirmed_prefix_tokens,
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
        anchor_confirmed = spans.get(anchor_plan.confirmed_message_id)
        if anchor_confirmed is not None:
            breakpoints.append(
                _breakpoint_from_span(
                    "anchor_confirmed",
                    anchor_confirmed,
                    anchor_ttl,
                )
            )
        frontier_confirmed = spans.get(frontier_plan.confirmed_message_id)
        if frontier_confirmed is not None:
            breakpoints.append(
                _breakpoint_from_span(
                    "frontier_confirmed",
                    frontier_confirmed,
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
        )
        self._remember(plan, request=request)
        return plan

    def inject(self, encoded: EncodedRequest, plan: PromptCachePlan) -> EncodedRequest:
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
        return EncodedRequest(
            payload=payload,
            message_spans=encoded.message_spans,
            extra_body=extra_body,
            applied_cache_breakpoint_message_ids=tuple(applied_breakpoint_message_ids),
        )

    def record_success(
        self,
        plan: PromptCachePlan,
        usage: LLMUsageIR,
        *,
        applied_cache_breakpoint_message_ids: tuple[str, ...] = (),
    ) -> None:
        if not plan.enabled:
            return
        applied_breakpoint_ids = frozenset(applied_cache_breakpoint_message_ids)
        with self._lock:
            stats = self._stats.setdefault(plan.scope_key, _ScopeStats())
            now = time.monotonic()
            stats.last_access_at = now
            self._prune_scopes_locked(now=now, keep=plan.scope_key)
            if usage.reported:
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
                "dialect": plan.dialect.value if plan is not None else "none",
                "decision": plan.decision if plan is not None else "not_planned",
                "estimated_prefix_tokens": (
                    plan.estimated_prefix_tokens if plan is not None else 0
                ),
                "breakpoints": (
                    [item.label for item in plan.breakpoints] if plan is not None else []
                ),
                "confirmed_checkpoint": bool(
                    last_stats
                    and (
                        last_stats.anchor.confirmed_message_id
                        or last_stats.frontier.confirmed_message_id
                    )
                ),
                "candidate_checkpoint": bool(
                    plan and plan.candidate_message_id
                ),
                "confirmed_prefix_tokens": (
                    max(
                        last_stats.anchor.confirmed_prefix_tokens,
                        last_stats.frontier.confirmed_prefix_tokens,
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
                "scope_count": len(self._stats),
                "last_scope_key": plan.scope_key if plan is not None else "",
                "anchor": _track_snapshot(
                    plan.anchor if plan is not None else None,
                    last_stats.anchor if last_stats else None,
                ),
                "frontier": _track_snapshot(
                    plan.frontier if plan is not None else None,
                    last_stats.frontier if last_stats else None,
                ),
            }

    def warm_deadline_snapshot(
        self,
        *,
        logical_scope_id: str = "",
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        """Expose the current confirmed A lifetime without leaking its prefix."""

        with self._lock:
            matching = [
                (self._stats.get(scope_key), plan)
                for scope_key, plan in self._last_plans_by_scope.items()
                if _scope_key_matches(
                    scope_key,
                    logical_scope_id=logical_scope_id,
                    endpoint_id=endpoint_id,
                )
            ]
            matching = [item for item in matching if item[0] is not None]
            if not matching:
                return {
                    "eligible": False,
                    "anchor_epoch": "",
                    "anchor_ttl_seconds": 0,
                    "prefix_tokens": 0,
                }
            stats, plan = max(
                matching,
                key=lambda item: item[0].last_access_at,
            )
            anchor = stats.anchor if stats is not None else None
            confirmed_fingerprint = (
                anchor.confirmed_fingerprint if anchor is not None else ""
            )
            confirmed_message_id = (
                anchor.confirmed_message_id if anchor is not None else ""
            )
            ttl_seconds = _ttl_seconds(plan.anchor.ttl)
            remaining_ttl_seconds = max(
                0,
                int(
                    ttl_seconds
                    - max(
                        0.0,
                        time.monotonic()
                        - (anchor.last_used_at if anchor is not None else 0.0),
                    )
                ),
            )
            # The reminder is specifically about confirmed A.  Candidate A
            # and the same-turn B frontier may be larger, but neither is a
            # durable prefix that the idle compact request can rely on.
            prefix_tokens = (
                anchor.confirmed_prefix_tokens if anchor is not None else 0
            )
            eligible = bool(
                _supports_explicit_breakpoints(plan.dialect)
                and confirmed_message_id
                and confirmed_fingerprint
                and ttl_seconds > 0
            )
            epoch_material = "|".join(
                (
                    plan.scope_key,
                    confirmed_message_id,
                    confirmed_fingerprint,
                    str(anchor.confirmed_sequence if anchor is not None else 0),
                )
            )
            return {
                "eligible": eligible,
                "anchor_epoch": (
                    hashlib.sha256(epoch_material.encode("utf-8")).hexdigest()[:24]
                    if eligible
                    else ""
                ),
                "anchor_ttl": plan.anchor.ttl,
                "anchor_ttl_seconds": ttl_seconds,
                "anchor_remaining_ttl_seconds": remaining_ttl_seconds,
                "prefix_tokens": prefix_tokens,
                "dialect": plan.dialect.value,
            }

    def confirmed_anchor_request(
        self,
        *,
        logical_scope_id: str = "",
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        """Return the exact in-memory request prefix through confirmed A.

        This is deliberately ephemeral.  It is used by resident compaction to
        replay provider-identical bytes without persisting prompt material.
        """

        with self._lock:
            candidates = [
                (self._stats.get(scope_key), plan)
                for scope_key, plan in self._last_plans_by_scope.items()
                if _scope_key_matches(
                    scope_key,
                    logical_scope_id=logical_scope_id,
                    endpoint_id=endpoint_id,
                )
            ]
            candidates = [
                (stats, plan)
                for stats, plan in candidates
                if stats is not None
                and stats.anchor.confirmed_message_id
                and stats.anchor.confirmed_fingerprint
            ]
            if not candidates:
                return {}
            stats, plan = max(
                candidates,
                key=lambda item: item[0].last_access_at,
            )
            ttl_seconds = _ttl_seconds(plan.anchor.ttl)
            if (
                stats.anchor.last_used_at <= 0
                or ttl_seconds <= 0
                or time.monotonic() - stats.anchor.last_used_at
                >= ttl_seconds
            ):
                return {}
            request = self._last_requests_by_scope.get(plan.scope_key)
            if request is None:
                return {}
            anchor_id = stats.anchor.confirmed_message_id
            anchor_index = next(
                (
                    index
                    for index, message in enumerate(request.messages)
                    if message.message_id == anchor_id
                ),
                None,
            )
            if anchor_index is None:
                return {}
            scope_parts = plan.scope_key.split("|")
            return {
                "request": replace(
                    request,
                    messages=request.messages[: anchor_index + 1],
                    metadata={
                        **dict(request.metadata),
                        "preferred_endpoint_id": (
                            scope_parts[1] if len(scope_parts) > 1 else ""
                        ),
                        "preferred_model_id": (
                            scope_parts[2] if len(scope_parts) > 2 else ""
                        ),
                        "preferred_endpoint_source": "prompt_cache_replay",
                        "endpoint_fallback_policy": "strict_preferred",
                    },
                ),
                "anchor_message_id": anchor_id,
                "anchor_fingerprint": stats.anchor.confirmed_fingerprint,
                "prefix_tokens": stats.anchor.confirmed_prefix_tokens,
                "dialect": plan.dialect.value,
                "wire_shape": (
                    scope_parts[3]
                    if len(scope_parts) > 3
                    else ""
                ),
                "scope_key": plan.scope_key,
            }

    def _remember(
        self,
        plan: PromptCachePlan,
        *,
        request: LLMRequestIR,
    ) -> None:
        with self._lock:
            self._last_plan = plan
            self._last_plans_by_scope[plan.scope_key] = plan
            # Raw prompt replay is useful only for the singleton resident
            # conversation.  Retaining full Bunshin requests for every scope
            # would turn this small policy index into an accidental prompt
            # archive with O(scope * context) memory use.
            if plan.scope_key.split("|", 1)[0] == "pal:resident":
                self._last_requests_by_scope[plan.scope_key] = request

    def _prune_scopes_locked(self, *, now: float, keep: str = "") -> None:
        for scope_key, stats in tuple(self._stats.items()):
            stats.prune(now=now, ttl_seconds=self.observation_ttl_seconds)
            if (
                scope_key != keep
                and not stats.observations
                and now - stats.last_access_at > self.observation_ttl_seconds
            ):
                self._stats.pop(scope_key, None)
                self._last_plans_by_scope.pop(scope_key, None)
                self._last_requests_by_scope.pop(scope_key, None)
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
            self._last_plans_by_scope.pop(scope_key, None)
            self._last_requests_by_scope.pop(scope_key, None)


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
    if not stats.confirmed_message_id:
        return False
    span = spans.get(stats.confirmed_message_id)
    if (
        span is None
        or not span.cache_prefix_fingerprint
        or span.cache_prefix_fingerprint != stats.confirmed_fingerprint
    ):
        stats.clear()
        return True
    stats.confirmed_prefix_tokens = span.estimated_cache_prefix_tokens
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
        "confirmed_message_id": stats.confirmed_message_id,
        "confirmed_fingerprint": stats.confirmed_fingerprint,
        "confirmed_prefix_tokens": stats.confirmed_prefix_tokens,
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
        stats.confirmed_message_id == target.message_id
        and stats.confirmed_fingerprint == target.cache_prefix_fingerprint
    ):
        return PromptCacheTrackPlan(
            **target_fields,
            decision="confirmed_reuse",
        )

    base_tokens = max(stats.confirmed_prefix_tokens, fallback_prefix_tokens)
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


def _record_track_success(
    stats: _TrackStats,
    plan: PromptCacheTrackPlan,
    *,
    plan_sequence: int,
    applied_breakpoint_ids: frozenset[str],
    observed_at: float,
) -> None:
    if plan_sequence < stats.confirmed_sequence:
        return
    if (
        plan.candidate_message_id
        and plan.candidate_message_id in applied_breakpoint_ids
    ):
        stats.confirmed_message_id = plan.candidate_message_id
        stats.confirmed_fingerprint = plan.target_fingerprint
        stats.confirmed_prefix_tokens = plan.target_prefix_tokens
        stats.confirmed_sequence = plan_sequence
        stats.last_used_at = observed_at
        stats.accumulated_reprocessed_tokens = 0
        return
    if (
        plan.confirmed_message_id
        and plan.confirmed_message_id == stats.confirmed_message_id
        and plan.confirmed_fingerprint == stats.confirmed_fingerprint
    ):
        stats.last_used_at = observed_at
    if plan.reprocessed_delta_tokens <= 0:
        return
    if (
        plan.confirmed_message_id != stats.confirmed_message_id
        or plan.confirmed_fingerprint != stats.confirmed_fingerprint
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
        "confirmed": bool(stats and stats.confirmed_message_id),
        "candidate": bool(plan and plan.candidate_message_id),
        "confirmed_prefix_tokens": stats.confirmed_prefix_tokens if stats else 0,
        "target_prefix_tokens": plan.target_prefix_tokens if plan else 0,
        "candidate_delta_tokens": plan.reprocessed_delta_tokens if plan else 0,
        "accumulated_reprocessed_tokens": (
            stats.accumulated_reprocessed_tokens if stats else 0
        ),
        "projected_reprocessed_tokens": (
            plan.projected_reprocessed_tokens if plan else 0
        ),
        "estimated_net_tokens": plan.estimated_net_tokens if plan else 0.0,
    }


def _resolve_dialect(context: ShapeContext) -> PromptCacheDialect:
    override = _prompt_cache_capabilities(context.capabilities)
    if override.get("enabled") is False:
        return PromptCacheDialect.NONE
    requested = str(override.get("dialect") or "").strip().lower()
    if requested:
        try:
            return PromptCacheDialect(requested)
        except ValueError:
            return PromptCacheDialect.NONE

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
    match = re.search(r"gpt-(\d+)\.(\d+)", normalized)
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (5, 6))


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
        )
    )


def _scope_key_matches(
    scope_key: str,
    *,
    logical_scope_id: str = "",
    endpoint_id: str = "",
) -> bool:
    parts = str(scope_key or "").split("|", 2)
    return not (
        (logical_scope_id and (not parts or parts[0] != logical_scope_id))
        or (endpoint_id and (len(parts) < 2 or parts[1] != endpoint_id))
    )


def _cache_key(request: LLMRequestIR, context: ShapeContext) -> str:
    material = json.dumps(
        {
            "scope": request.logical_scope_id or "default",
            "endpoint": context.endpoint_id,
            "model": context.model_id,
            "shape": context.wire_shape.value,
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


def _ttl_seconds(value: str) -> int:
    normalized = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d+)([smh])", normalized)
    if match is None:
        return 0
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    return max(0, amount * multiplier)
