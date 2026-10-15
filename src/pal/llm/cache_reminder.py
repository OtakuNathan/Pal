"""Local cache-lifetime estimates for idle reminders, never cache evidence."""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from pal.llm.ir import LLMRequestIR, LLMUsageIR
from pal.llm.shapes.base import EncodedRequest, ShapeContext


# These are scheduling estimates. Providers may evict earlier; none of these
# values authorizes the confirmed-hot compaction path. Unknown models inherit
# their provider policy. Add an exact model rule when its lifetime differs.
DEFAULT_REMINDER_TTL_SECONDS = 30 * 60
PROVIDER_TTL_SECONDS: Mapping[str, int] = MappingProxyType({
    "openai": 30 * 60,
    "openrouter": 30 * 60,
    "anthropic": 5 * 60,
    # DeepSeek documents hours to days, not a fixed lifetime. One hour is a
    # deliberately earlier local reminder, not a provider expiration claim.
    "deepseek": 60 * 60,
})
MODEL_TTL_SECONDS: Mapping[tuple[str, str], int] = MappingProxyType({})
_REQUEST_TTL_SECONDS = {"5m": 300, "30m": 1800, "1h": 3600}


def reminder_ttl(provider: str, model: str, *, requested_ttls: tuple[str, ...] = ()) -> tuple[int, str]:
    requested = [_REQUEST_TTL_SECONDS[value] for value in requested_ttls if value in _REQUEST_TTL_SECONDS]
    if requested:
        return min(requested), "request_cache_ttl"
    provider = provider.strip().lower()
    model_rule = MODEL_TTL_SECONDS.get((provider, model))
    if model_rule is not None:
        return model_rule, "model_policy"
    if provider in PROVIDER_TTL_SECONDS:
        return PROVIDER_TTL_SECONDS[provider], "provider_policy"
    return DEFAULT_REMINDER_TTL_SECONDS, "fallback_policy"


def reminder_binding(*, endpoint_id: str, model_id: str, provider_id: str,
                     wire_shape: str, base_url: str) -> str:
    # No URL, prompt, credential reference or payload is exposed in a snapshot.
    return hashlib.sha256(json.dumps(
        [endpoint_id, model_id, provider_id.lower(), wire_shape, base_url],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


@dataclass(frozen=True)
class CacheReminderBasis:
    logical_scope_id: str
    binding: str
    endpoint_id: str
    model_id: str
    provider_id: str
    ttl_seconds: int
    ttl_source: str
    estimated_context_tokens: int
    planned_at: float
    observation_id: str


def reminder_basis(request: LLMRequestIR, context: ShapeContext, encoded: EncodedRequest,
                   *, mode: str, requested_ttls: tuple[str, ...] = ()) -> CacheReminderBasis | None:
    if mode == "disabled" or not request.logical_scope_id:
        return None
    ttl, source = reminder_ttl(context.provider_id, context.model_id, requested_ttls=requested_ttls)
    return CacheReminderBasis(
        logical_scope_id=request.logical_scope_id,
        binding=reminder_binding(endpoint_id=context.endpoint_id, model_id=context.model_id,
            provider_id=context.provider_id, wire_shape=context.wire_shape.value, base_url=context.base_url),
        endpoint_id=context.endpoint_id, model_id=context.model_id, provider_id=context.provider_id,
        ttl_seconds=ttl, ttl_source=source,
        estimated_context_tokens=max((span.estimated_cache_prefix_tokens for span in encoded.message_spans), default=0),
        planned_at=time.monotonic(), observation_id=uuid4().hex,
    )


@dataclass(frozen=True)
class _ReturnedResponse:
    basis: CacheReminderBasis
    returned_at: float
    returned_at_utc: str
    context_tokens: int
    tokens_source: str


class CacheReminderTracker:
    """Owned under the coordinator lock; retain only bounded response metadata."""

    def __init__(self) -> None:
        self._responses: OrderedDict[tuple[str, str], _ReturnedResponse] = OrderedDict()

    def record_success(self, basis: CacheReminderBasis | None, usage: LLMUsageIR, *, maximum: int) -> None:
        if basis is None:
            return
        key = basis.logical_scope_id, basis.binding
        previous = self._responses.get(key)
        if previous is not None and (
            previous.basis.observation_id == basis.observation_id
            or previous.basis.planned_at > basis.planned_at
        ):
            return
        reported = usage.has("input_tokens")
        self._responses[key] = _ReturnedResponse(
            basis, time.monotonic(), datetime.now(timezone.utc).isoformat(),
            max(0, int(usage.input_tokens)) if reported else basis.estimated_context_tokens,
            "provider_usage" if reported else "local_estimate",
        )
        self._responses.move_to_end(key)
        while len(self._responses) > max(1, maximum):
            self._responses.popitem(last=False)

    def snapshot(self, *, logical_scope_id: str, binding: str) -> dict[str, Any]:
        response = self._responses.get((logical_scope_id, binding))
        if response is None:
            return {"eligible": False, "estimated": True, "reason": "no_successful_response"}
        basis = response.basis
        remaining = max(0.0, basis.ttl_seconds - (time.monotonic() - response.returned_at))
        return {
            "eligible": remaining > 0, "estimated": True,
            "reason": "estimated_response_lifetime" if remaining > 0 else "estimated_lifetime_elapsed",
            "anchor_epoch": basis.observation_id, "anchor_ttl_seconds": basis.ttl_seconds,
            "anchor_remaining_ttl_seconds": remaining,
            "context_tokens": response.context_tokens, "prefix_tokens": response.context_tokens,
            "tokens_source": response.tokens_source, "ttl_source": basis.ttl_source,
            "endpoint_id": basis.endpoint_id, "model_id": basis.model_id, "provider_id": basis.provider_id,
            "response_returned_at": response.returned_at_utc,
        }
