from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Mapping

from pal.llm import cache_wire as wire
from pal.llm.cache_tail import TailHistory, POLICY
from pal.llm.cache_policy import (
    CacheProfile, CacheProfileError, CACHE_PROFILES, PromptCacheDialect,
    resolve_profile as _resolve_profile, resolve_dialect as _resolve_dialect,
    resolve_mode, OPENAI_EXPLICIT, validate_cache_policy,
)

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


@dataclass(frozen=True)
class PromptCacheBreakpoint:
    label: str
    message_id: str
    ttl: str = "5m"
    prefix_tokens: int = 0
    path: tuple[str | int, ...] = ()


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
    prepared_encoded: EncodedRequest | None = field(default=None, repr=False, compare=False)
    tail_generation: int = 0
    tail_current: wire.Boundary | None = None
    mode: str = "implicit"
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


_ANCHOR_TTL_SECONDS = {"5m": 300.0, "30m": 1800.0, "1h": 3600.0}


@dataclass
class _AnchorReadEvidence:
    """Anchor-writing request retained for READ-evidence confirmation.

    dffb210 removed submitted-marker authorization: bytes the planner once
    sent may never authorize a cache-dependent request by themselves.  The
    entry stays unconfirmed until a LATER successful request's provider
    usage reports the anchor prefix served FROM CACHE
    (``cached_input_tokens >= prefix_tokens``) — the provider's own read
    evidence.  Retention is the resident singleton only.
    """

    request: LLMRequestIR
    anchor_message_id: str
    anchor_fingerprint: str
    prefix_tokens: int
    submitted_sequence: int
    submitted_at: float
    endpoint_id: str
    wire_shape: str
    dialect: str
    ttl_seconds: float
    confirmed_at: float = 0.0

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at > 0.0


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
    _tails: dict[str, TailHistory] = field(default_factory=dict, init=False, repr=False)
    _stats: dict[str, _ScopeStats] = field(default_factory=dict, init=False, repr=False)
    _anchor_evidence: dict[str, _AnchorReadEvidence] = field(
        default_factory=dict, init=False, repr=False
    )
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
        dialect = _resolve_dialect(context)
        profile_fields = {
            "profile_id": profile.profile_id if profile else "",
            "strategy": profile.strategy if profile else "",
            "profile_version": profile.version if profile else "",
            "profile_origin": str(_prompt_cache_capabilities(context.capabilities).get("origin") or ("endpoint" if profile else "default")),
            "profile_generation": str(_prompt_cache_capabilities(context.capabilities).get("generation") or "0"),
        }
        scope_key = _scope_key(request, context, dialect)
        mode = resolve_mode(context)
        if dialect in OPENAI_EXPLICIT and mode in {"explicit", "hybrid"}:
            return self._plan_tail(request, context, encoded, dialect, profile_fields, mode)
        if mode in {"implicit", "disabled"}:
            with self._lock:
                stats = self._stats.setdefault(scope_key, _ScopeStats())
                stats.next_plan_sequence += 1
                stats.last_access_at = time.monotonic()
                sequence = stats.next_plan_sequence
                self._prune_scopes_locked(now=stats.last_access_at, keep=scope_key)
            reusable = {m.message_id for m in request.messages if m.prompt_region != PromptRegionIR.ACTIVE_DYNAMIC}
            plan = PromptCachePlan(scope_key=scope_key, cache_key=_cache_key(request, context),
                dialect=dialect, mode=mode, plan_sequence=sequence,
                estimated_prefix_tokens=max((s.estimated_cache_prefix_tokens for s in encoded.message_spans if s.message_id in reusable), default=0),
                decision="provider_automatic" if mode == "implicit" else "disabled",
                **profile_fields)
            self._remember(plan, request=request, context=context)
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
            mode="explicit",
        )
        self._remember(plan, request=request, context=context)
        return plan

    def _plan_tail(self, request, context, encoded, dialect, profile_fields, mode):
        clean = wire.clean_request(encoded)
        spans = {s.message_id: s for s in clean.message_spans}
        def boundary(span):
            if span is None or not span.cache_targets:
                return None
            return wire.Boundary(span.message_id, span.cache_targets[-1],
                                   span.cache_prefix_fingerprint, span.estimated_cache_prefix_tokens)
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
        key_request = replace(request, metadata={**request.metadata, "turn_id": "", "artifact_turn_id": ""})
        scope = _scope_key(key_request, context, dialect) + "|" + wire.digest(
            {"strategy": POLICY, "mode": mode, "binding": _prompt_cache_capabilities(context.capabilities)})
        with self._lock:
            state = self._tails.setdefault(scope, TailHistory())
            now = time.monotonic()
            turn = str(request.metadata.get("turn_id") or request.metadata.get("artifact_turn_id") or "")
            continuity = str(request.metadata.get("continuity_id") or "")
            if continuity != state.continuity_id:
                state.continuity_id, state.continuity_turn = continuity, turn
            if continuity:
                for message in request.messages:
                    span = spans.get(message.message_id)
                    if span and span.continuity_target and message.metadata.get("continuity_id") == continuity:
                        compact = wire.boundary_at(clean, message.message_id, span.continuity_target)
                        if compact and (state.continuity_turn == turn or anchor is None):
                            anchor = compact
                        break
            valid = {b.path: wire.boundary_at(clean, b.message_id, b.path)
                     for b in state.tails if b.message_id in spans
                     and b.path in spans[b.message_id].cache_targets}
            epoch = (stable.fingerprint if stable else "", anchor.fingerprint if anchor else "")
            state.refresh(turn, epoch, valid, now)
            estimated_prefix = max((b.coordinate for b in (anchor, frontier) if b), default=0)
            if (mode != "explicit" or state.closed or frontier is None
                    or anchor is not None and frontier.path <= anchor.path):
                frontier = None
            previous = state.previous(frontier)
            points = []
            for label, b in (("stable", stable), ("anchor_fixed", anchor),
                             ("tail_previous", previous), ("tail_current", frontier)):
                if b is not None and b.path not in {p.path for p in points}:
                    points.append(PromptCacheBreakpoint(label, b.message_id, "30m", b.coordinate, b.path))
            plan = PromptCachePlan(scope_key=scope, cache_key=_cache_key(request, context),
                dialect=dialect, breakpoints=tuple(points), mode=mode,
                decision="eager_tail" if mode == "explicit" else "hybrid_fixed_anchors",
                plan_sequence=state.sequence + 1,
                estimated_prefix_tokens=estimated_prefix,
                prepared_encoded=clean, tail_generation=state.generation, tail_current=frontier,
                **dict(profile_fields, strategy=POLICY if mode == "explicit" else "fixed_anchors"))
            self._prune_scopes_locked(now=now, keep=scope)
            self._remember(plan, request=request, context=context)
            return plan

    def end_turn(self, turn_id: str) -> None:
        with self._lock:
            for state in self._tails.values():
                state.end_turn(turn_id)

    def inject(self, encoded: EncodedRequest, plan: PromptCachePlan) -> EncodedRequest:
        if plan.prepared_encoded is not None:
            return wire.inject_exact(plan.prepared_encoded, plan.breakpoints, plan.cache_key,
                plan.dialect == PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
                prepared=True, mode=plan.mode)
        if plan.mode in {"implicit", "disabled"}:
            clean = wire.clean_request(encoded, normalize_chat=False)
            extra = {}
            if plan.mode != "disabled":
                if plan.dialect == PromptCacheDialect.OPENAI_AUTOMATIC:
                    extra["prompt_cache_key"] = plan.cache_key
                elif plan.dialect == PromptCacheDialect.OPENROUTER_AUTOMATIC:
                    extra["session_id"] = plan.cache_key
                    if plan.profile_id:
                        extra["prompt_cache_key"] = plan.cache_key
            return replace(clean, extra_body=extra)
        if not plan.enabled:
            return encoded
        payload = thaw_json(encoded.payload)
        extra_body = thaw_json(encoded.extra_body)
        targets = {
            span.message_id: span.cache_targets
            for span in encoded.message_spans
        }
        applied_breakpoint_message_ids: list[str] = []

        if plan.dialect == PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT:
            extra_body["session_id"] = plan.cache_key
        if _uses_anthropic_breakpoints(plan.dialect):
            for breakpoint in plan.breakpoints:
                marker = {"type": "ephemeral", "ttl": breakpoint.ttl}
                if _mark_last_target(payload, targets.get(breakpoint.message_id, ()), "cache_control", marker):
                    applied_breakpoint_message_ids.append(breakpoint.message_id)
        return EncodedRequest(
            payload=payload,
            message_spans=encoded.message_spans,
            extra_body=extra_body,
            applied_cache_breakpoint_message_ids=tuple(applied_breakpoint_message_ids),
        )

    def discard_accounted_attempt(self, plan: PromptCachePlan, request_id: str) -> None:
        """Usage ledger owns receipt deduplication; tail history has no pending ACK."""
        return

    def prepare_attempt(self, request: LLMRequestIR, context: ShapeContext,
                        raw_encoded: EncodedRequest, request_id: str):
        # Serialize planning and submission of the bounded tail history.
        with self._lock:
            plan = self.plan(request, context, raw_encoded)
            encoded = self.inject(raw_encoded, plan)
            diagnostics = self.start_attempt(plan, request=request, context=context,
                encoded=encoded, raw_encoded=raw_encoded, request_id=request_id)
            return plan, encoded, diagnostics

    def start_attempt(self, plan: PromptCachePlan, *, request: LLMRequestIR,
                      context: ShapeContext, encoded: EncodedRequest,
                      raw_encoded: EncodedRequest, request_id: str) -> dict[str, Any]:
        if plan.prepared_encoded is not None:
            with self._lock:
                state = self._tails.get(plan.scope_key)
                expected = tuple(b.path for b in plan.breakpoints)
                audited, wire_hash = wire.audit(encoded, expected, mode=plan.mode)
                actual_clean = wire.clean_request(encoded, finalize=False)
                audited &= all(wire.boundary_at(actual_clean, b.message_id, b.path)
                               == wire.boundary_at(plan.prepared_encoded, b.message_id, b.path)
                               for b in plan.breakpoints)
                audited &= encoded.extra_body.get("prompt_cache_key") == plan.cache_key
                if plan.dialect == PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT:
                    audited &= encoded.extra_body.get("session_id") == plan.cache_key
                audited &= state is not None and state.generation == plan.tail_generation and not state.closed
                if not audited:
                    raise CacheProfileError("cache marker payload or content generation changed before submission")
                state.submit(plan.tail_current, plan.tail_generation, plan.plan_sequence)
                tail_snapshot = state.snapshot()
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
            if plan.prepared_encoded is not None:
                stats.next_plan_sequence = plan.plan_sequence
            for track, track_plan in (() if plan.prepared_encoded is not None else ((stats.anchor, plan.anchor), (stats.frontier, plan.frontier))):
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
                "prompt_log_enabled": bool(request.metadata.get("prompt_log_enabled")),
            }
        if plan.prepared_encoded is not None:
            diagnostics.update(cache_tail=tail_snapshot, wire_audit_fingerprint=wire_hash)
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
            "cache_mode": plan.mode,
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
                    "path": list(item.path),
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
            "cache_tail": diagnostics.get("cache_tail"),
            "wire_audit_fingerprint": diagnostics.get("wire_audit_fingerprint", ""),
            "prompt_cache_diagnostics": diagnostics.get("prompt_cache_diagnostics"),
            "recorded_at": time.time(),
        }
        with self._lock:
            previous = next((item for item in self._attempt_records if request_id and item["attempt_id"] == request_id), None)
            if previous is not None:
                if previous["status"] != "submitted":
                    return
                if not record["wire_audit_fingerprint"]:
                    record["wire_audit_fingerprint"] = previous.get("wire_audit_fingerprint", "")
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
            log_enabled = bool(diagnostics.get("prompt_log_enabled") or
                               previous and previous.get("prompt_log_enabled"))
            record["prompt_log_enabled"] = log_enabled
            if previous is not None:
                previous["prompt_log_enabled"] = log_enabled
            log_record = dict(previous if previous is not None else record) if log_enabled else {}
        if log_enabled and plan.prepared_encoded is not None:
            event = {key: log_record.get(key) for key in (
                "attempt_id", "status", "turn_id", "round_index", "cache_mode", "strategy", "planned_markers",
                "applied_marker_paths", "wire_audit_fingerprint", "cache_tail",
                "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "reported_fields",
                "cache_key_hash", "session_key_hash")}
            for counter in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens"):
                if counter not in log_record.get("reported_fields", ()):
                    event[counter] = None
            logger.info("prompt_cache_tail %s", json.dumps(event, ensure_ascii=False))

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
            self._prune_scopes_locked(now=now, keep=self._last_plan.scope_key if self._last_plan else plan.scope_key)
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
                # READ-evidence anchor confirmation (dffb210 boundary): the
                # provider itself must report the anchor prefix served from
                # cache on a LATER request — strictly after the write.
                evidence = self._anchor_evidence.get(plan.scope_key)
                if (
                    evidence is not None
                    and not evidence.confirmed
                    and evidence.anchor_fingerprint
                    == plan.anchor.submitted_fingerprint
                    and evidence.prefix_tokens > 0
                    and int(plan.plan_sequence) > evidence.submitted_sequence
                    and usage.has("cached_input_tokens")
                    and int(usage.cached_input_tokens)
                    >= evidence.prefix_tokens
                ):
                    evidence.confirmed_at = now
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
                "tail": self._tails[plan.scope_key].snapshot() if plan and plan.scope_key in self._tails else None,
                "dialect": plan.dialect.value if plan is not None else "none",
                "mode": plan.mode if plan is not None else "implicit",
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
                "scope_count": len(set(self._stats) | set(self._tails)),
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
        """READ-evidence anchor confirmation (dffb210 boundary honored).

        Returns the anchor-writing request ONLY when the provider has since
        reported serving that anchor's prefix from cache on a later
        successful request (``cached_input_tokens >= prefix_tokens``), the
        confirmation is inside the anchor's TTL, and the requested endpoint
        matches.  Submitted marker bytes alone authorize nothing.
        """

        wanted_scope = str(logical_scope_id or "pal:resident").strip()
        with self._lock:
            now = time.monotonic()
            for scope_key, evidence in list(self._anchor_evidence.items()):
                if str(
                    getattr(evidence.request, "logical_scope_id", "") or ""
                ) != wanted_scope:
                    continue
                if endpoint_id and str(endpoint_id) != evidence.endpoint_id:
                    continue
                if not evidence.confirmed:
                    continue
                if now - evidence.confirmed_at > evidence.ttl_seconds:
                    self._anchor_evidence.pop(scope_key, None)
                    continue
                return {
                    "request": evidence.request,
                    "anchor_message_id": evidence.anchor_message_id,
                    "dialect": evidence.dialect,
                    "wire_shape": evidence.wire_shape,
                }
            return {}

    def eligible_anchor_request(self, *, logical_scope_id: str = "", endpoint_id: str = "") -> dict[str, Any]:
        """Local-eligibility anchor replay (R3/S1, review 95373ef).

        The anchor-writing request is locally valid material for building a
        same-source handoff as soon as the bytes exist and the scope /
        endpoint / TTL still hold — READ evidence is NOT a prerequisite for
        trying: a hit saves the prefix, a miss costs exactly what the cold
        alternative would have paid.  ``read_confirmed`` exposes the READ
        observation for diagnostics and explicit hot-only surfaces; callers
        must not turn it into a permission check for ordinary autocompact.
        Submitted marker bytes still authorize nothing on their own
        (dffb210): this reader returns material for a request that treats
        the anchor as input, never a cache-dependent guarantee.
        """

        wanted_scope = str(logical_scope_id or "pal:resident").strip()
        with self._lock:
            now = time.monotonic()
            for scope_key, evidence in list(self._anchor_evidence.items()):
                if str(
                    getattr(evidence.request, "logical_scope_id", "") or ""
                ) != wanted_scope:
                    continue
                if endpoint_id and str(endpoint_id) != evidence.endpoint_id:
                    continue
                if now - evidence.submitted_at > evidence.ttl_seconds:
                    self._anchor_evidence.pop(scope_key, None)
                    continue
                return {
                    "request": evidence.request,
                    "anchor_message_id": evidence.anchor_message_id,
                    "dialect": evidence.dialect,
                    "wire_shape": evidence.wire_shape,
                    "read_confirmed": bool(
                        evidence.confirmed
                        and now - evidence.confirmed_at <= evidence.ttl_seconds
                    ),
                }
            return {}

    def _remember(
        self,
        plan: PromptCachePlan,
        *,
        request: LLMRequestIR,
        context: ShapeContext | None = None,
    ) -> None:
        with self._lock:
            self._last_plan = plan
            self._retain_anchor_evidence_locked(
                plan, request=request, context=context)

    def _retain_anchor_evidence_locked(
        self,
        plan: PromptCachePlan,
        *,
        request: LLMRequestIR,
        context: ShapeContext | None,
    ) -> None:
        """Retain/refresh the anchor-writing request (resident only).

        Same anchor re-submitted → keep the ORIGINAL write request (same
        bytes, earliest sequence).  A different anchor → replace with a
        fresh unconfirmed write.  Anchor no longer planned → the stale
        entry dies: submitted markers authorize nothing on their own.
        """

        existing = self._anchor_evidence.get(plan.scope_key)
        retainable = (
            plan.enabled
            and plan.anchor.submitted_message_id
            and context is not None
            and str(getattr(request, "logical_scope_id", "") or "")
            == "pal:resident"
        )
        if not retainable:
            if (
                existing is not None
                and not plan.anchor.submitted_message_id
                and plan.anchor.submitted_fingerprint != existing.anchor_fingerprint
            ):
                self._anchor_evidence.pop(plan.scope_key, None)
            return
        if (
            existing is not None
            and existing.anchor_fingerprint == plan.anchor.submitted_fingerprint
            and existing.anchor_message_id == plan.anchor.submitted_message_id
        ):
            return
        self._anchor_evidence[plan.scope_key] = _AnchorReadEvidence(
            request=request,
            anchor_message_id=str(plan.anchor.submitted_message_id),
            anchor_fingerprint=str(plan.anchor.submitted_fingerprint),
            prefix_tokens=max(0, int(plan.anchor.submitted_prefix_tokens or 0)),
            submitted_sequence=int(plan.plan_sequence),
            submitted_at=time.monotonic(),
            endpoint_id=str(context.endpoint_id if context is not None else ""),
            wire_shape=str(context.wire_shape.value if context is not None else ""),
            dialect=str(plan.dialect.value),
            ttl_seconds=_ANCHOR_TTL_SECONDS.get(
                str(plan.anchor.ttl or "5m"), 300.0
            ),
        )

    def _prune_scopes_locked(self, *, now: float, keep: str = "") -> None:
        for stats in self._stats.values():
            stats.prune(now=now, ttl_seconds=self.observation_ttl_seconds)
        scopes = set(self._stats) | set(self._tails)
        def accessed(key):
            return max(self._stats[key].last_access_at if key in self._stats else 0,
                       self._tails[key].last_access if key in self._tails else 0)
        maximum = max(1, int(self.max_scope_count))
        for key in sorted(scopes, key=accessed):
            if key != keep and (now - accessed(key) > self.observation_ttl_seconds or len(scopes) > maximum):
                self._stats.pop(key, None)
                self._tails.pop(key, None)
                self._anchor_evidence.pop(key, None)
                scopes.remove(key)


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
