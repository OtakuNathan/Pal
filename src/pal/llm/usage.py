from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from pal.llm.ir import LLMUsageIR


@dataclass
class _UsageBucket:
    endpoint_id: str = ""
    model_id: str = ""
    provider: str = ""
    successful_request_count: int = 0
    failed_request_count: int = 0
    provider_response_count: int = 0
    failed_attempt_count: int = 0
    usage_reported_request_count: int = 0
    cache_hit_request_count: int = 0
    usage: LLMUsageIR = field(default_factory=LLMUsageIR)

    def record_success(self, usage: LLMUsageIR, *, provider_response_count: int) -> None:
        self.successful_request_count += 1
        self.provider_response_count += max(1, int(provider_response_count or 0))
        self.usage_reported_request_count += int(usage.reported)
        self.cache_hit_request_count += int(usage.cached_input_tokens > 0)
        self.usage = _add_usage(self.usage, usage)

    def record_failed_attempt(self) -> None:
        self.failed_attempt_count += 1

    def to_dict(self) -> dict[str, Any]:
        request_count = self.successful_request_count + self.failed_request_count
        usage_reporting_rate = (
            self.usage_reported_request_count / self.successful_request_count
            if self.successful_request_count > 0
            else 0.0
        )
        return {
            "endpoint_id": self.endpoint_id,
            "model_id": self.model_id,
            "provider": self.provider,
            "request_count": request_count,
            "successful_request_count": self.successful_request_count,
            "failed_request_count": self.failed_request_count,
            "provider_request_count": self.provider_response_count + self.failed_attempt_count,
            "provider_response_count": self.provider_response_count,
            "failed_attempt_count": self.failed_attempt_count,
            "usage_reported_request_count": self.usage_reported_request_count,
            "usage_reporting_rate": usage_reporting_rate,
            "cache_hit_request_count": self.cache_hit_request_count,
            "request_cache_hit_rate": (
                self.cache_hit_request_count / self.successful_request_count
                if self.successful_request_count > 0
                else 0.0
            ),
            **_usage_to_dict(self.usage),
        }


class LLMUsageLedger:
    """Thread-safe, process-lifetime aggregate for the resident LLM runtime."""

    def __init__(self, *, scope: str = "resident_process") -> None:
        self.scope = str(scope or "resident_process")
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._total = _UsageBucket()
        self._by_endpoint: dict[str, _UsageBucket] = {}
        self._lock = RLock()

    def record_success(
        self,
        *,
        endpoint_id: str,
        model_id: str,
        provider: str,
        usage: LLMUsageIR,
        provider_response_count: int,
    ) -> None:
        with self._lock:
            bucket = self._endpoint_bucket(endpoint_id, model_id=model_id, provider=provider)
            bucket.record_success(usage, provider_response_count=provider_response_count)
            self._total.record_success(usage, provider_response_count=provider_response_count)

    def record_failed_attempt(
        self,
        *,
        endpoint_id: str,
        model_id: str,
        provider: str,
    ) -> None:
        with self._lock:
            bucket = self._endpoint_bucket(endpoint_id, model_id=model_id, provider=provider)
            bucket.record_failed_attempt()
            self._total.record_failed_attempt()

    def record_failed_request(self, *, endpoint_id: str = "") -> None:
        with self._lock:
            self._total.failed_request_count += 1
            if endpoint_id and endpoint_id in self._by_endpoint:
                self._by_endpoint[endpoint_id].failed_request_count += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._total.to_dict()
            return {
                "scope": self.scope,
                "started_at": self.started_at,
                **total,
                "by_endpoint": [
                    self._by_endpoint[endpoint_id].to_dict()
                    for endpoint_id in sorted(self._by_endpoint)
                ],
            }

    def _endpoint_bucket(self, endpoint_id: str, *, model_id: str, provider: str) -> _UsageBucket:
        normalized = str(endpoint_id or "").strip() or "unknown"
        bucket = self._by_endpoint.get(normalized)
        if bucket is None:
            bucket = _UsageBucket(
                endpoint_id=normalized,
                model_id=str(model_id or ""),
                provider=str(provider or ""),
            )
            self._by_endpoint[normalized] = bucket
        return bucket


def _add_usage(left: LLMUsageIR, right: LLMUsageIR) -> LLMUsageIR:
    return LLMUsageIR(
        input_tokens=left.input_tokens + right.input_tokens,
        uncached_input_tokens=left.uncached_input_tokens + right.uncached_input_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        cache_write_input_tokens=(
            left.cache_write_input_tokens + right.cache_write_input_tokens
        ),
        output_tokens=left.output_tokens + right.output_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        cost=left.cost + right.cost,
        reported=left.reported or right.reported,
    )


def _usage_to_dict(usage: LLMUsageIR) -> dict[str, Any]:
    input_tokens = max(0, int(usage.input_tokens))
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": max(0, int(usage.uncached_input_tokens)),
        "cached_input_tokens": max(0, int(usage.cached_input_tokens)),
        "cache_write_input_tokens": max(
            0,
            int(usage.cache_write_input_tokens),
        ),
        "output_tokens": max(0, int(usage.output_tokens)),
        "reasoning_tokens": max(0, int(usage.reasoning_tokens)),
        "cost": max(0.0, float(usage.cost)),
        "reported": bool(usage.reported),
        "cache_hit_rate": (
            max(0, int(usage.cached_input_tokens)) / input_tokens
            if input_tokens > 0
            else 0.0
        ),
        "token_cache_ratio": (
            max(0, int(usage.cached_input_tokens)) / input_tokens
            if input_tokens > 0
            else 0.0
        ),
        "cache_write_ratio": (
            max(0, int(usage.cache_write_input_tokens)) / input_tokens
            if input_tokens > 0
            else 0.0
        ),
    }
