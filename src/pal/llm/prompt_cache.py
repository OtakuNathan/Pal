from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from typing import Any, Mapping

from pal.llm.ir import LLMRequestIR, LLMUsageIR, PromptRegionIR, WireShape
from pal.llm.shapes.base import EncodedRequest, ShapeContext
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


@dataclass(frozen=True)
class PromptCachePlan:
    scope_key: str
    cache_key: str
    dialect: PromptCacheDialect
    breakpoints: tuple[PromptCacheBreakpoint, ...] = ()
    decision: str = "disabled"
    estimated_prefix_tokens: int = 0

    @property
    def enabled(self) -> bool:
        return self.dialect != PromptCacheDialect.NONE


@dataclass
class _ScopeStats:
    eligible_requests: int = 0
    observations: deque[tuple[float, int, int]] = field(
        default_factory=lambda: deque(maxlen=8)
    )
    last_decision: str = ""
    last_access_at: float = field(default_factory=time.monotonic)

    @property
    def observed_requests(self) -> int:
        return len(self.observations)

    @property
    def cache_hit_requests(self) -> int:
        return sum(cached > 0 for _, cached, _ in self.observations)

    @property
    def cached_tokens(self) -> int:
        return sum(cached for _, cached, _ in self.observations)

    @property
    def cache_write_tokens(self) -> int:
        return sum(written for _, _, written in self.observations)

    def prune(self, *, now: float, ttl_seconds: float) -> None:
        while self.observations and now - self.observations[0][0] > ttl_seconds:
            self.observations.popleft()

    @property
    def predicted_net_tokens(self) -> float:
        # Coarse provider-independent economics: a cached read saves roughly
        # 90%; an explicit write costs roughly a 25% premium.
        return self.cached_tokens * 0.9 - self.cache_write_tokens * 0.25


@dataclass
class PromptCacheCoordinator:
    """Own provider cache policy; core only labels semantic prompt regions."""

    minimum_prefix_tokens: int = 1024
    rolling_threshold_tokens: int = 4096
    rolling_net_threshold_tokens: int = 1024
    reprobe_every: int = 4
    observation_ttl_seconds: float = 30.0 * 60.0
    max_scope_count: int = 256
    _stats: dict[str, _ScopeStats] = field(default_factory=dict, init=False, repr=False)
    _last_plan: PromptCachePlan | None = field(default=None, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def plan(self, request: LLMRequestIR, context: ShapeContext) -> PromptCachePlan:
        dialect = _resolve_dialect(context)
        scope_key = _scope_key(request, context, dialect)
        if dialect == PromptCacheDialect.NONE:
            plan = PromptCachePlan(
                scope_key=scope_key,
                cache_key="",
                dialect=dialect,
            )
            self._remember(plan)
            return plan

        stable = [
            message
            for message in request.messages
            if message.prompt_region == PromptRegionIR.STABLE_SYSTEM
        ]
        settled = [
            message
            for message in request.messages
            if message.prompt_region == PromptRegionIR.SETTLED_HISTORY
        ]
        active = [
            message
            for message in request.messages
            if message.prompt_region == PromptRegionIR.ACTIVE_INPUT
        ]
        estimated_prefix_tokens = _estimate_request_prefix_tokens(request, through=active[:1])
        minimum = _capability_int(
            context.capabilities,
            "minimum_tokens",
            self.minimum_prefix_tokens,
        )
        rolling_threshold = _capability_int(
            context.capabilities,
            "rolling_threshold_tokens",
            self.rolling_threshold_tokens,
        )

        explicit_breakpoints = _supports_explicit_breakpoints(dialect)
        breakpoints: list[PromptCacheBreakpoint] = []
        stable_prefix_tokens = _estimate_request_prefix_tokens(
            request,
            through=stable[-1:],
        )
        if explicit_breakpoints and stable and stable_prefix_tokens >= minimum:
            breakpoints.append(
                PromptCacheBreakpoint(
                    "stable",
                    stable[-1].message_id,
                    "1h" if _uses_anthropic_breakpoints(dialect) else "30m",
                )
            )

        rolling_eligible = (
            explicit_breakpoints
            and bool(active)
            and estimated_prefix_tokens >= rolling_threshold
        )
        rolling_enabled = False
        decision = "stable_only" if explicit_breakpoints else "provider_automatic"
        with self._lock:
            now = time.monotonic()
            stats = self._stats.setdefault(scope_key, _ScopeStats())
            stats.last_access_at = now
            self._prune_scopes_locked(now=now, keep=scope_key)
            if rolling_eligible:
                stats.eligible_requests += 1
                rolling_enabled = (
                    stats.observed_requests == 0
                    or stats.predicted_net_tokens >= self.rolling_net_threshold_tokens
                    or stats.eligible_requests % max(1, self.reprobe_every) == 0
                )
                decision = "rolling_enabled" if rolling_enabled else "rolling_not_economic"
            elif explicit_breakpoints and not breakpoints:
                decision = "below_provider_minimum"
            stats.last_decision = decision

        if rolling_enabled:
            settled_candidates = (
                settled
                if _supports_arbitrary_message_blocks(dialect)
                else [message for message in settled if message.role.value == "user"]
            )
            if settled_candidates:
                breakpoints.append(
                    PromptCacheBreakpoint(
                        "settled",
                        settled_candidates[-1].message_id,
                    )
                )
            breakpoints.append(PromptCacheBreakpoint("active", active[0].message_id))

        plan = PromptCachePlan(
            scope_key=scope_key,
            cache_key=_cache_key(request, context),
            dialect=dialect,
            breakpoints=tuple(breakpoints),
            decision=decision,
            estimated_prefix_tokens=estimated_prefix_tokens,
        )
        self._remember(plan)
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
                _mark_last_target(
                    payload,
                    targets.get(breakpoint.message_id, ()),
                    "prompt_cache_breakpoint",
                    {"mode": "explicit"},
                )
        elif _uses_anthropic_breakpoints(plan.dialect):
            for breakpoint in plan.breakpoints:
                marker: dict[str, Any] = {"type": "ephemeral"}
                if breakpoint.ttl in {"5m", "1h"}:
                    marker["ttl"] = breakpoint.ttl
                _mark_last_target(
                    payload,
                    targets.get(breakpoint.message_id, ()),
                    "cache_control",
                    marker,
                )
        return EncodedRequest(payload, encoded.message_spans, extra_body)

    def observe(self, plan: PromptCachePlan, usage: LLMUsageIR) -> None:
        if not plan.enabled or not usage.reported:
            return
        with self._lock:
            stats = self._stats.setdefault(plan.scope_key, _ScopeStats())
            now = time.monotonic()
            stats.last_access_at = now
            self._prune_scopes_locked(now=now, keep=plan.scope_key)
            stats.observations.append(
                (
                    now,
                    max(0, int(usage.cached_input_tokens)),
                    max(0, int(usage.cache_write_input_tokens)),
                )
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            plan = self._last_plan
            now = time.monotonic()
            self._prune_scopes_locked(now=now)
            observed = sum(item.observed_requests for item in self._stats.values())
            hit_requests = sum(item.cache_hit_requests for item in self._stats.values())
            return {
                "dialect": plan.dialect.value if plan is not None else "none",
                "decision": plan.decision if plan is not None else "not_planned",
                "estimated_prefix_tokens": (
                    plan.estimated_prefix_tokens if plan is not None else 0
                ),
                "breakpoints": (
                    [item.label for item in plan.breakpoints] if plan is not None else []
                ),
                "observed_request_count": observed,
                "request_hit_rate": hit_requests / observed if observed else 0.0,
                "scope_count": len(self._stats),
                "last_scope_key": plan.scope_key if plan is not None else "",
            }

    def _remember(self, plan: PromptCachePlan) -> None:
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


def _supports_arbitrary_message_blocks(dialect: PromptCacheDialect) -> bool:
    return _uses_anthropic_breakpoints(dialect)


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


def _estimate_request_prefix_tokens(
    request: LLMRequestIR,
    *,
    through: list[Any],
) -> int:
    if not through:
        return _estimate_messages_tokens(list(request.messages))
    target_id = through[-1].message_id
    prefix: list[Any] = []
    for message in request.messages:
        prefix.append(message)
        if message.message_id == target_id:
            break
    tool_chars = sum(
        len(tool.name) + len(tool.description) + len(json.dumps(thaw_json(tool.input_schema)))
        for tool in request.tools
    )
    return max(1, (_estimate_messages_chars(prefix) + tool_chars + 3) // 4)


def _estimate_messages_tokens(messages: list[Any]) -> int:
    return max(0, (_estimate_messages_chars(messages) + 3) // 4)


def _estimate_messages_chars(messages: list[Any]) -> int:
    return sum(
        len(message.role.value)
        + sum(
            len(str(getattr(part, "text", getattr(part, "content", ""))))
            for part in message.parts
        )
        for message in messages
    )


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
