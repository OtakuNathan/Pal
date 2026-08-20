from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from pal.llm.contracts import (
    LLMGenerationResult,
    LLMPreflightAdvice,
    LLMPreflightRequest,
    LLMRuntimePort,
    ThinkingChoice,
    ThinkingContract,
)
from pal.llm.credentials import LLMCredentialResolver, LLMCredentialUnavailableError
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import (
    GenerationPolicyIR,
    ImagePartIR,
    LLMFinishReason,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseIR,
    LLMResponseDeltaKind,
    LLMResponseUpdate,
    MessageRole,
    MessageState,
    TextPartIR,
    ThinkingLevel,
    WireShape,
)
from pal.llm.model_hooks import ModelHookRegistry
from pal.llm.models import LLMEndpointModel
from pal.llm.output_recovery import (
    continuation_request,
    has_committed_tool_calls,
    endpoint_output_upper_limit,
    merge_responses,
    recovery_settings,
    safe_truncated_response,
    stream_recovery_updates,
    with_recovery_stage,
)
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.response_hooks import (
    ProviderResponseHookError,
    ProviderResponseHookRegistry,
)
from pal.llm.usage import LLMUsageLedger
from pal.llm.transport import (
    DirectSDKTransport,
    LLMEndpointSpecStaleError,
    LLMProviderStartedError,
    LLMStreamControl,
    LLMStreamCancelledError,
)
from pal.shared import LLMPreflightStatus
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR


_DEFAULT_TIMEOUT_SECONDS = 600.0
_STRICT_ENDPOINT_PREFERRED_SOURCES = frozenset({"profile"})
_FALLBACK_DISABLED_POLICIES = frozenset(
    {"disabled", "none", "off", "strict", "strict_preferred", "no_fallback"}
)
class LLMEndpointInvocationError(RuntimeError):
    pass


class LLMEndpointResponseError(LLMEndpointInvocationError):
    pass


class LLMRequestPreparationError(LLMEndpointInvocationError):
    pass


@dataclass(frozen=True)
class PreparedLLMRequest:
    endpoint: LLMEndpointModel
    request: LLMRequestIR
    estimated_input_tokens: int
    target_input_budget: int

    @property
    def compact_required(self) -> bool:
        return (
            self.target_input_budget > 0
            and self.estimated_input_tokens > self.target_input_budget
        )


@dataclass
class EndpointResolver:
    repository: LLMEndpointRepository | None = None
    endpoints: tuple[LLMEndpointModel, ...] = ()

    def __post_init__(self) -> None:
        if self.endpoints:
            self.endpoints = tuple(self.endpoints)
            self._validate()
        else:
            self.refresh()

    def refresh(self) -> tuple[LLMEndpointModel, ...]:
        if self.repository is not None:
            self.endpoints = tuple(self.repository.list_enabled())
        self._validate()
        return self.endpoints

    def _validate(self) -> None:
        for endpoint in self.endpoints:
            LLMEndpointSpec.from_value(endpoint)

    def enabled(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        fallback_endpoint_id: str | None = None,
        include_remaining: bool = True,
    ) -> list[LLMEndpointModel]:
        items = list(self.endpoints)
        ordered: list[LLMEndpointModel] = []
        seen: set[str] = set()
        for endpoint_id in (preferred_endpoint_id, fallback_endpoint_id):
            normalized = str(endpoint_id or "").strip()
            if not normalized or normalized in seen:
                continue
            match = next((item for item in items if item.endpoint_id == normalized), None)
            if match is not None:
                ordered.append(match)
                seen.add(normalized)
        if include_remaining:
            ordered.extend(item for item in items if item.endpoint_id not in seen)
        return ordered if ordered else (items if include_remaining else items[:1])

    def primary(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        fallback_endpoint_id: str | None = None,
    ) -> LLMEndpointModel | None:
        enabled = self.enabled(
            preferred_endpoint_id=preferred_endpoint_id,
            fallback_endpoint_id=fallback_endpoint_id,
        )
        return enabled[0] if enabled else None


class LLMEndpointInvokerPort(Protocol):
    def invoke(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        stream: bool = False,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]:
        ...

    def invoke_updates(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        stream_control: LLMStreamControl | None = None,
    ) -> Iterator[LLMResponseUpdate]:
        ...


def build_default_endpoint_invoker(
    *,
    credentials: LLMCredentialResolver | None = None,
    runtime_root: str | Path | None = None,
    response_hooks: ProviderResponseHookRegistry | None = None,
) -> ShapeEndpointInvoker:
    _ = runtime_root
    resolver = credentials or LLMCredentialResolver()
    return ShapeEndpointInvoker(
        transport=DirectSDKTransport(
            credential_resolver=resolver.resolve_api_key,
        ),
        response_hooks=response_hooks or ProviderResponseHookRegistry.builtin(),
    )


@dataclass
class LLMRuntime(LLMRuntimePort):
    endpoint_resolver: EndpointResolver
    settings_repository: RuntimeSettingRepository
    endpoint_invoker: LLMEndpointInvokerPort | None = None
    config: Any = None
    safety_margin_tokens: int = 16_384
    endpoint_retry_attempts: int = 3
    last_request: LLMRequestIR | None = None
    last_endpoint_id: str | None = None
    last_model_id: str | None = None
    think_level: str = ""
    active_endpoint_id: str | None = None
    event_sink: Callable[[dict[str, Any]], None] | None = None
    usage_ledger: LLMUsageLedger = field(default_factory=LLMUsageLedger, repr=False)
    model_hooks: ModelHookRegistry = field(init=False)
    provider_response_hooks: ProviderResponseHookRegistry = field(init=False)
    _detached_stream_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        runtime_root = Path(getattr(self.config, "runtime_root", None) or ".")
        self.model_hooks = ModelHookRegistry.load(runtime_root)
        self.provider_response_hooks = ProviderResponseHookRegistry.builtin()
        if self.endpoint_invoker is None:
            self.endpoint_invoker = build_default_endpoint_invoker(
                runtime_root=runtime_root,
                response_hooks=self.provider_response_hooks,
            )
        elif isinstance(self.endpoint_invoker, ShapeEndpointInvoker):
            # Initial decoding and post-continuation normalization are two
            # passes through one immutable provider-response pipeline.
            self.provider_response_hooks = self.endpoint_invoker.response_hooks
        self.endpoint_retry_attempts = max(
            1,
            int(getattr(self.config, "llm_endpoint_retry_attempts", self.endpoint_retry_attempts) or 1),
        )
        self.refresh_runtime_settings()

    def active_endpoint(self) -> LLMEndpointModel | None:
        return self.endpoint_resolver.primary(preferred_endpoint_id=self.active_endpoint_id)

    def refresh_runtime_settings(self) -> None:
        previous = self.active_endpoint()
        configured = self.settings_repository.get_active_llm_endpoint_id()
        endpoint_ids = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        self.active_endpoint_id = configured if configured in endpoint_ids else None
        endpoint = self.active_endpoint()
        self.think_level = self._effective_thinking_level(endpoint) or ""
        if endpoint is not None and (
            previous is None or previous.endpoint_id != endpoint.endpoint_id
        ):
            activate = getattr(self.endpoint_invoker, "activate_endpoint", None)
            if callable(activate):
                activate(endpoint.endpoint_id)

    def refresh_llm_endpoints(self) -> dict[str, Any]:
        before = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        self.endpoint_resolver.refresh()
        refresh_settings = getattr(self.settings_repository, "refresh", None)
        if callable(refresh_settings):
            refresh_settings()
        runtime_root = Path(getattr(self.config, "runtime_root", None) or ".")
        self.model_hooks = ModelHookRegistry.load(runtime_root)
        refresh_credentials = getattr(
            self.endpoint_invoker,
            "refresh_credentials",
            None,
        )
        credentials_refreshed = bool(
            refresh_credentials()
            if callable(refresh_credentials)
            else False
        )
        self.refresh_runtime_settings()
        after = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        primary = self.active_endpoint()
        return {
            "before_count": len(before),
            "enabled_count": len(after),
            "added_endpoint_ids": sorted(after - before),
            "removed_endpoint_ids": sorted(before - after),
            "configured_active_endpoint_id": self.settings_repository.get_active_llm_endpoint_id(),
            "active_endpoint_id": self.active_endpoint_id,
            "primary_endpoint_id": primary.endpoint_id if primary else None,
            "primary_model_id": primary.model_id if primary else None,
            "model_hook_count": len(self.model_hooks.hooks),
            "credentials_refreshed": credentials_refreshed,
        }

    def set_active_endpoint(self, endpoint_id: str) -> str:
        normalized = str(endpoint_id or "").strip()
        if not any(endpoint.endpoint_id == normalized for endpoint in self.endpoint_resolver.endpoints):
            raise ValueError(f"unknown enabled LLM endpoint: {normalized}")
        self.settings_repository.set_active_llm_endpoint_id(normalized)
        self.active_endpoint_id = normalized
        self.think_level = self._effective_thinking_level(self.active_endpoint()) or ""
        activate = getattr(self.endpoint_invoker, "activate_endpoint", None)
        if callable(activate):
            activate(normalized)
        return normalized

    def close(self) -> None:
        close = getattr(self.endpoint_invoker, "close", None)
        if callable(close):
            close()

    def thinking_contract(self, endpoint_id: str | None = None) -> ThinkingContract | None:
        endpoint = self._endpoint_by_id(endpoint_id) if endpoint_id else self.active_endpoint()
        if endpoint is None:
            return None
        levels = self._thinking_levels(endpoint)
        if not levels:
            return None
        return ThinkingContract(
            choices=tuple(ThinkingChoice(level, level.replace("xhigh", "extra high").title()) for level in levels),
            default_choice_id=str(endpoint.default_thinking_level),
        )

    def thinking_status(self, endpoint_id: str | None = None) -> dict[str, Any]:
        endpoint = self._endpoint_by_id(endpoint_id) if endpoint_id else self.active_endpoint()
        if endpoint is None:
            return {"available": False, "endpoint_id": None, "model_id": None, "current": None, "choices": []}
        levels = self._thinking_levels(endpoint)
        return {
            "available": bool(levels),
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "current": self._effective_thinking_level(endpoint),
            "choices": [{"id": level, "label": level.replace("xhigh", "extra high").title()} for level in levels],
        }

    def thinking_levels_snapshot(self) -> dict[str, str]:
        return {
            endpoint.endpoint_id: level
            for endpoint in self.endpoint_resolver.enabled()
            if (level := self._effective_thinking_level(endpoint)) is not None
        }

    def set_think_level(self, value: str, *, endpoint_id: str | None = None) -> str:
        endpoint = self._endpoint_by_id(endpoint_id) if endpoint_id else self.active_endpoint()
        if endpoint is None:
            raise ValueError("no enabled LLM endpoint is available")
        normalized = str(value or "").strip().lower()
        levels = self._thinking_levels(endpoint)
        if normalized not in levels:
            raise ValueError(f"invalid think level for {endpoint.endpoint_id}; available: {', '.join(levels)}")
        self.settings_repository.set_think_level(endpoint.endpoint_id, normalized)
        if endpoint.endpoint_id == self.active_endpoint_id:
            self.think_level = normalized
        return normalized

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        self.refresh_runtime_settings()
        endpoints = self._enabled_endpoints(request.request)
        endpoint = endpoints[0] if endpoints else None
        if endpoint is None:
            raise LLMEndpointInvocationError("no enabled endpoints are available")
        prepared = self._compile_request(endpoint, request.request)
        return LLMPreflightAdvice(
            status=(
                LLMPreflightStatus.COMPACT_REQUIRED
                if prepared.compact_required
                else LLMPreflightStatus.READY
            ),
            active_model=endpoint.model_id,
            fallback_chain=[item.model_id for item in endpoints[1:]],
            target_input_budget=prepared.target_input_budget,
            reserved_output_tokens=prepared.request.policy.max_output_tokens,
            breakdown={
                "estimated_input_tokens": prepared.estimated_input_tokens,
                "target_input_budget": prepared.target_input_budget,
            },
        )

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return self.preflight(request)

    def resolve_endpoint_facts(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> dict[str, Any]:
        endpoints = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint = endpoints[0] if endpoints else None
        if endpoint is None:
            return {
                "endpoint_id": str(preferred_endpoint_id or self.active_endpoint_id or "") or None,
                "model_id": None,
                "wire_shape": None,
                "context_window": None,
                "max_output_tokens": None,
                "supports_streaming": False,
                "supports_tools": False,
                "supports_vision": False,
                "input_modalities": [],
                "output_modalities": [],
                "capabilities": {},
                "thinking_levels": [],
                "default_thinking_level": None,
            }
        return {
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "wire_shape": endpoint.wire_shape,
            "context_window": endpoint.context_window,
            "max_output_tokens": endpoint.max_output_tokens,
            "max_output_tokens_upper_limit": endpoint_output_upper_limit(endpoint),
            "supports_streaming": bool(endpoint.supports_streaming),
            "supports_tools": bool(endpoint.supports_tools),
            "supports_vision": bool(endpoint.supports_vision),
            "input_modalities": list(endpoint.input_modalities_blob or ()),
            "output_modalities": list(endpoint.output_modalities_blob or ()),
            "capabilities": dict(endpoint.capabilities_blob or {}),
            "thinking_levels": list(endpoint.thinking_levels_blob or ()),
            "default_thinking_level": endpoint.default_thinking_level,
        }

    def resolve_max_output_tokens(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> int | None:
        endpoints = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint = endpoints[0] if endpoints else None
        if endpoint is None:
            return None
        if endpoint.max_output_tokens is not None:
            return int(endpoint.max_output_tokens)
        return int(endpoint.context_window) if endpoint.context_window is not None else None

    def supports_streaming(self, request: LLMRequestIR | None = None) -> bool:
        endpoints = self._enabled_endpoints(request) if request is not None else [self.active_endpoint()]
        endpoint = endpoints[0] if endpoints else None
        return bool(endpoint and endpoint.supports_streaming)

    def generate(self, request: LLMRequestIR) -> LLMGenerationResult:
        return self._generate(request, allow_stale_refresh=True)

    def _generate(
        self,
        request: LLMRequestIR,
        *,
        allow_stale_refresh: bool,
    ) -> LLMGenerationResult:
        self.last_request = request
        try:
            endpoints = self._enabled_endpoints(request)
        except Exception as exc:
            self.usage_ledger.record_failed_request()
            return _failure_result(str(exc), exc=exc)
        if not endpoints:
            return _failure_result("no enabled endpoints are available")
        last_error: Exception | None = None
        requested_preferred = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        for endpoint_index, endpoint in enumerate(endpoints):
            try:
                prepared = self._compile_request(endpoint, request)
            except Exception as exc:
                last_error = exc
                error_kind = self._record_failure(endpoint, exc, 0)
                self._emit(
                    "llm_endpoint_exhausted",
                    endpoint=endpoint,
                    reason=error_kind,
                )
                continue
            effective = prepared.request
            if prepared.compact_required:
                return self._compact_required_result(endpoint, effective)
            if _is_stub_endpoint(endpoint):
                response = _text_response("stub response", LLMFinishReason.STUB)
                return self._success(endpoint, response)
            for attempt in range(self.endpoint_retry_attempts):
                try:
                    response, _ = self._invoker().invoke(
                        endpoint,
                        effective,
                        stream=False,
                        timeout_seconds=self._timeout_seconds(effective),
                    )
                    if response.finish_reason == LLMFinishReason.LENGTH:
                        response = self._recover_length(endpoint, effective, response)
                        # Recovery merges multiple already-decoded pieces, so
                        # the merged response crosses the same provider
                        # decorator once more. Non-recovered responses were
                        # normalized by the endpoint iterator already.
                        response = self._normalize_completed_response(
                            endpoint,
                            effective,
                            response,
                        )
                    if response.finish_reason == LLMFinishReason.ERROR:
                        raise LLMEndpointResponseError(
                            f"endpoint {endpoint.endpoint_id} returned finish_reason=error"
                        )
                    if requested_preferred is None and endpoint.endpoint_id != self.active_endpoint_id:
                        self.set_active_endpoint(endpoint.endpoint_id)
                    if endpoint_index > 0:
                        self._emit("llm_endpoint_fallback_succeeded", endpoint=endpoint)
                    return self._success(endpoint, response)
                except LLMEndpointSpecStaleError as exc:
                    if allow_stale_refresh:
                        self._emit(
                            "llm_endpoint_spec_refresh",
                            endpoint=endpoint,
                            reason="endpoint_spec_stale",
                        )
                        self.refresh_llm_endpoints()
                        return self._generate(
                            request,
                            allow_stale_refresh=False,
                        )
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    retryable = _retryable_error_kind(error_kind)
                    if retryable and attempt + 1 < self.endpoint_retry_attempts:
                        time.sleep(_retry_delay(attempt + 1))
                        continue
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
        self.usage_ledger.record_failed_request(
            endpoint_id=endpoints[-1].endpoint_id if endpoints else ""
        )
        return _failure_result(
            _public_failure_text(last_error),
            exc=last_error,
        )

    async def agenerate(self, request: LLMRequestIR) -> LLMGenerationResult:
        return await asyncio.to_thread(self.generate, request)

    def _iter_stream_updates(
        self,
        request: LLMRequestIR,
        *,
        stream_control: LLMStreamControl | None = None,
        allow_stale_refresh: bool = True,
    ) -> Iterator[LLMResponseUpdate]:
        self.last_request = request
        try:
            endpoints = self._enabled_endpoints(request)
        except Exception as exc:
            self.usage_ledger.record_failed_request()
            response = _failure_result(str(exc), exc=exc).response
            yield LLMResponseUpdate(
                response,
                delta_kind=LLMResponseDeltaKind.STATE,
            )
            return
        if not endpoints:
            response = _failure_result("no enabled endpoints are available").response
            yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)
            return
        last_error: Exception | None = None
        for endpoint in endpoints:
            try:
                prepared = self._compile_request(endpoint, request)
            except Exception as exc:
                last_error = exc
                error_kind = self._record_failure(endpoint, exc, 0)
                self._emit(
                    "llm_endpoint_exhausted",
                    endpoint=endpoint,
                    reason=error_kind,
                )
                continue
            effective = prepared.request
            if prepared.compact_required:
                response = self._compact_required_result(endpoint, effective).response
                yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)
                return
            semantic_seen = False
            for attempt in range(self.endpoint_retry_attempts):
                last_update: LLMResponseUpdate | None = None
                try:
                    invoke_kwargs: dict[str, Any] = {
                        "timeout_seconds": self._timeout_seconds(effective),
                    }
                    if isinstance(self._invoker(), ShapeEndpointInvoker):
                        invoke_kwargs["stream_control"] = stream_control
                    for update in self._invoker().invoke_updates(
                        endpoint,
                        effective,
                        **invoke_kwargs,
                    ):
                        last_update = update
                        semantic_seen = semantic_seen or (
                            update.delta_kind != LLMResponseDeltaKind.STATE
                            and bool(update.response.message.parts)
                        )
                        if (
                            update.delta_kind == LLMResponseDeltaKind.STATE
                            and update.response.finish_reason
                            in {LLMFinishReason.LENGTH, LLMFinishReason.ERROR}
                        ):
                            continue
                        yield update
                    completed = last_update.response if last_update is not None else _text_response("", LLMFinishReason.ERROR)
                    if completed.finish_reason == LLMFinishReason.LENGTH:
                        recovered = self._recover_length(
                            endpoint,
                            effective,
                            completed,
                            allow_discarded_retry=False,
                        )
                        recovered = self._normalize_completed_response(
                            endpoint,
                            effective,
                            recovered,
                        )
                        if recovered.finish_reason == LLMFinishReason.ERROR:
                            raise LLMEndpointResponseError(
                                f"endpoint {endpoint.endpoint_id} output recovery returned finish_reason=error"
                            )
                        recovery_updates = tuple(stream_recovery_updates(completed, recovered))
                        yield from recovery_updates
                        completed = recovered
                    if completed.finish_reason == LLMFinishReason.ERROR:
                        raise LLMEndpointResponseError(
                            f"endpoint {endpoint.endpoint_id} returned finish_reason=error"
                        )
                    self._record_success(endpoint, completed)
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    return
                except LLMEndpointSpecStaleError as exc:
                    if allow_stale_refresh and not semantic_seen and not bool(
                        stream_control is not None
                        and stream_control.provider_started
                    ):
                        self._emit(
                            "llm_endpoint_spec_refresh",
                            endpoint=endpoint,
                            reason="endpoint_spec_stale",
                        )
                        self.refresh_llm_endpoints()
                        yield from self._iter_stream_updates(
                            request,
                            stream_control=stream_control,
                            allow_stale_refresh=False,
                        )
                        return
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    error_kind = self._record_failure(endpoint, exc, attempt)
                    provider_started = semantic_seen or bool(
                        stream_control is not None
                        and stream_control.provider_started
                    )
                    if provider_started:
                        partial = (
                            last_update.response
                            if last_update is not None
                            else _text_response(
                                _public_failure_text(exc),
                                LLMFinishReason.ERROR,
                            )
                        )
                        error_response = _response_with_failure(
                            partial,
                            exc,
                            partial_output_chars=(
                                len(str(partial.message.text or ""))
                                if semantic_seen
                                else 0
                            ),
                        )
                        self.usage_ledger.record_failed_request(
                            endpoint_id=endpoint.endpoint_id
                        )
                        yield LLMResponseUpdate(error_response, delta_kind=LLMResponseDeltaKind.STATE)
                        return
                    if (
                        _retryable_error_kind(error_kind)
                        and attempt + 1 < self.endpoint_retry_attempts
                    ):
                        time.sleep(_retry_delay(attempt + 1))
                        continue
                    self._emit(
                        "llm_endpoint_exhausted",
                        endpoint=endpoint,
                        reason=error_kind,
                    )
                    break
        self.usage_ledger.record_failed_request(
            endpoint_id=endpoints[-1].endpoint_id if endpoints else ""
        )
        response = _failure_result(
            str(last_error or "LLM stream failed"),
            exc=last_error,
        ).response
        yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)

    async def astream(self, request: LLMRequestIR) -> AsyncIterator[LLMResponseUpdate]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        done = object()
        stream_control = LLMStreamControl()
        wall_timeout = self._stream_wall_timeout_seconds(request)
        cleanup_timeout = self._stream_cleanup_timeout_seconds()

        def enqueue(item: object) -> None:
            # A cooperatively cancelled SDK worker can outlive its consumer
            # until the provider read timeout expires.  The resident loop is
            # normally still present, but shutdown may close it first.
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass

        def worker() -> None:
            try:
                for update in self._iter_stream_updates(
                    request,
                    stream_control=stream_control,
                ):
                    enqueue(update)
            except BaseException as exc:  # noqa: BLE001
                enqueue(exc)
            finally:
                enqueue(done)

        task = asyncio.create_task(asyncio.to_thread(worker))
        deadline = loop.time() + wall_timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    stream_control.cancel("wall_timeout")
                    response = _failure_result(
                        f"LLM stream exceeded the {wall_timeout:g}s wall-clock limit"
                    ).response
                    yield LLMResponseUpdate(
                        response,
                        delta_kind=LLMResponseDeltaKind.STATE,
                    )
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=min(remaining, 1.0),
                    )
                except asyncio.TimeoutError:
                    continue
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item  # type: ignore[misc]
        finally:
            stream_control.cancel("consumer_closed")
            if not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=cleanup_timeout,
                    )
                except asyncio.TimeoutError:
                    # The SDK owns its hidden socket and must close the active
                    # response on the worker thread after its network timeout.
                    # Cancellation fences every later capability admission;
                    # tracking keeps the worker alive until its finally block
                    # releases the last hazard and closes the client graph.
                    self._track_detached_stream_task(task)
                except asyncio.CancelledError:
                    self._track_detached_stream_task(task)
                    raise
                except LLMStreamCancelledError:
                    pass
            elif not task.cancelled():
                try:
                    task.result()
                except LLMStreamCancelledError:
                    pass

    def _track_detached_stream_task(self, task: asyncio.Task[Any]) -> None:
        if task in self._detached_stream_tasks:
            return
        self._detached_stream_tasks.add(task)
        task.add_done_callback(self._retire_detached_stream_task)

    def _retire_detached_stream_task(self, task: asyncio.Task[Any]) -> None:
        self._detached_stream_tasks.discard(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    def usage_snapshot(self) -> dict[str, Any]:
        snapshot = self.usage_ledger.snapshot()
        prompt_cache = getattr(self.endpoint_invoker, "prompt_cache", None)
        cache_snapshot = getattr(prompt_cache, "snapshot", None)
        if callable(cache_snapshot):
            snapshot["prompt_cache_policy"] = cache_snapshot()
        return snapshot

    def _compile_request(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
    ) -> PreparedLLMRequest:
        LLMEndpointSpec.from_value(endpoint)
        hooked = self.model_hooks.apply(
            endpoint.model_id,
            replace(request, model_hint=endpoint.model_id),
        )
        level = hooked.policy.thinking_level
        levels = self._thinking_levels(endpoint)
        if level is None:
            effective = self._effective_thinking_level(endpoint)
            level = ThinkingLevel(effective) if effective else None
        elif level.value not in levels:
            raise LLMRequestPreparationError(
                f"thinking_level={level.value!r} is not declared by endpoint "
                f"{endpoint.endpoint_id}; available={list(levels)}"
            )
        max_output = hooked.policy.max_output_tokens
        if endpoint.max_output_tokens is not None:
            max_output = min(max_output, int(endpoint.max_output_tokens))
        policy = replace(
            hooked.policy,
            max_output_tokens=max_output,
            thinking_level=level,
        )
        prepared = replace(hooked, policy=policy, model_hint=endpoint.model_id)
        target = self._target_input_budget(endpoint, prepared.policy.max_output_tokens)
        return PreparedLLMRequest(
            endpoint=endpoint,
            request=prepared,
            estimated_input_tokens=_estimate_request_tokens(prepared),
            target_input_budget=target,
        )

    def _prepare_request(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
    ) -> LLMRequestIR:
        return self._compile_request(endpoint, request).request

    def _enabled_endpoints(self, request: LLMRequestIR | None) -> list[LLMEndpointModel]:
        metadata = dict(request.metadata) if request is not None else {}
        endpoints = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=str(metadata.get("preferred_endpoint_id") or "").strip() or None,
            preferred_endpoint_source=str(metadata.get("preferred_endpoint_source") or "").strip() or None,
            endpoint_fallback_policy=str(metadata.get("endpoint_fallback_policy") or "").strip() or None,
        )
        if request is not None and _request_has_image_input(request):
            return [endpoint for endpoint in endpoints if bool(endpoint.supports_vision)]
        return endpoints

    def _enabled_endpoints_for_preference(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
        endpoint_fallback_policy: str | None = None,
    ) -> list[LLMEndpointModel]:
        preferred = str(preferred_endpoint_id or "").strip() or None
        source = str(preferred_endpoint_source or "").strip().lower()
        strict = (
            str(endpoint_fallback_policy or "").strip().lower() in _FALLBACK_DISABLED_POLICIES
            or bool(preferred and source in _STRICT_ENDPOINT_PREFERRED_SOURCES)
        )
        if strict:
            selected = preferred or self.active_endpoint_id
            if selected:
                return [
                    endpoint
                    for endpoint in self.endpoint_resolver.endpoints
                    if endpoint.endpoint_id == selected
                ]
            return list(self.endpoint_resolver.endpoints[:1])
        return self.endpoint_resolver.enabled(
            preferred_endpoint_id=preferred,
            fallback_endpoint_id=self.active_endpoint_id,
            include_remaining=True,
        )

    def _effective_thinking_level(self, endpoint: LLMEndpointModel | None) -> str | None:
        if endpoint is None:
            return None
        levels = self._thinking_levels(endpoint)
        if not levels:
            return None
        persisted = str(self.settings_repository.get_think_level(endpoint.endpoint_id) or "").strip().lower()
        effective = (
            persisted
            if persisted in levels
            else str(getattr(endpoint, "default_thinking_level", None) or levels[0])
        )
        if effective not in levels:
            effective = levels[0]
        if persisted != effective:
            self.settings_repository.set_think_level(endpoint.endpoint_id, effective)
        return effective

    @staticmethod
    def _thinking_levels(endpoint: LLMEndpointModel) -> tuple[str, ...]:
        return LLMEndpointSpec.from_value(endpoint).thinking_levels_blob

    def _endpoint_by_id(self, endpoint_id: str | None) -> LLMEndpointModel | None:
        normalized = str(endpoint_id or "").strip()
        return next((endpoint for endpoint in self.endpoint_resolver.endpoints if endpoint.endpoint_id == normalized), None)

    def _needs_compaction(self, endpoint: LLMEndpointModel, request: LLMRequestIR) -> bool:
        target = self._target_input_budget(endpoint, request.policy.max_output_tokens)
        return target > 0 and _estimate_request_tokens(request) > target

    def _target_input_budget(self, endpoint: LLMEndpointModel, output_tokens: int) -> int:
        context_window = int(endpoint.context_window or 0)
        if context_window <= 0:
            return 0
        margin = min(self.safety_margin_tokens, max(1024, int(context_window * 0.05)))
        return max(1, context_window - int(output_tokens) - margin)

    def _compact_required_result(self, endpoint: LLMEndpointModel, request: LLMRequestIR) -> LLMGenerationResult:
        response = _text_response("Context compaction is required before LLM invocation.", LLMFinishReason.COMPACT_REQUIRED)
        return LLMGenerationResult(
            response=response,
            target_input_budget=self._target_input_budget(endpoint, request.policy.max_output_tokens),
            reserved_output_tokens=request.policy.max_output_tokens,
            preferred_endpoint_id=endpoint.endpoint_id,
            preferred_model_id=endpoint.model_id,
        )

    def _recover_length(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        first: LLMResponseIR,
        *,
        allow_discarded_retry: bool = True,
    ) -> LLMResponseIR:
        if first.finish_reason != LLMFinishReason.LENGTH:
            return first
        # A provider item boundary is a durable semantic commit.  Do not ask
        # the model to regenerate a closed tool call: return it to the normal
        # agent loop, which remains the sole execution path.
        if has_committed_tool_calls(first):
            return first
        default_attempts = max(
            0,
            int(
                getattr(
                    self.config,
                    "llm_max_output_recovery_attempts",
                    3,
                )
                or 0
            ),
        )
        settings = recovery_settings(
            endpoint,
            request,
            default_attempts=default_attempts,
        )
        if not settings.enabled:
            return safe_truncated_response(first)

        discarded: tuple[LLMResponseIR, ...] = ()
        responses: list[LLMResponseIR] = []
        current_request = request
        current = first
        if (
            allow_discarded_retry
            and request.policy.max_output_tokens < settings.upper_limit
        ):
            escalated = with_recovery_stage(
                request,
                stage="escalate",
                attempt=0,
                max_output_tokens=settings.upper_limit,
            )
            if not self._needs_compaction(endpoint, escalated):
                self._emit(
                    "llm_output_limit_recovery_started",
                    endpoint=endpoint,
                    stage="escalate",
                    attempt=0,
                    max_output_tokens=settings.upper_limit,
                )
                discarded = (first,)
                current_request = escalated
                current, _ = self._invoker().invoke(
                    endpoint,
                    current_request,
                    stream=False,
                    timeout_seconds=self._timeout_seconds(current_request),
                )
                if has_committed_tool_calls(current):
                    return merge_responses([current], discarded=discarded)
                if current.finish_reason != LLMFinishReason.LENGTH:
                    recovered = merge_responses([current], discarded=discarded)
                    self._emit(
                        "llm_output_limit_recovery_succeeded",
                        endpoint=endpoint,
                        stage="escalate",
                        attempt=0,
                        max_output_tokens=settings.upper_limit,
                    )
                    return recovered

        responses.append(safe_truncated_response(current))
        for attempt in range(1, settings.max_continuations + 1):
            candidate = continuation_request(
                current_request,
                current,
                max_output_tokens=settings.upper_limit,
                attempt=attempt,
            )
            if self._needs_compaction(endpoint, candidate):
                break
            self._emit(
                "llm_output_limit_recovery_started",
                endpoint=endpoint,
                stage="continue",
                attempt=attempt,
                max_output_tokens=settings.upper_limit,
            )
            current_request = candidate
            current, _ = self._invoker().invoke(
                endpoint,
                current_request,
                stream=False,
                timeout_seconds=self._timeout_seconds(current_request),
            )
            if has_committed_tool_calls(current):
                return merge_responses(
                    [*responses, current],
                    discarded=discarded,
                )
            if current.finish_reason != LLMFinishReason.LENGTH:
                recovered = merge_responses(
                    [*responses, current],
                    discarded=discarded,
                )
                self._emit(
                    "llm_output_limit_recovery_succeeded",
                    endpoint=endpoint,
                    stage="continue",
                    attempt=attempt,
                    max_output_tokens=settings.upper_limit,
                )
                return recovered
            responses.append(safe_truncated_response(current))

        exhausted = merge_responses(responses, discarded=discarded)
        self._emit(
            "llm_output_limit_recovery_exhausted",
            endpoint=endpoint,
            stage="continue",
            attempt=max(0, len(responses) - 1),
            max_output_tokens=settings.upper_limit,
        )
        return exhausted

    def _normalize_completed_response(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        response: LLMResponseIR,
    ) -> LLMResponseIR:
        updates = tuple(
            self.provider_response_hooks.normalize(
                endpoint_id=str(endpoint.endpoint_id),
                provider_id=str(endpoint.provider),
                model_id=str(endpoint.model_id),
                wire_shape=WireShape(str(endpoint.wire_shape)),
                request=request,
                updates=(
                    LLMResponseUpdate(
                        response=response,
                        delta_kind=LLMResponseDeltaKind.STATE,
                    ),
                ),
            )
        )
        if not updates:
            raise ProviderResponseHookError(
                f"provider response hook produced no output for {endpoint.endpoint_id}"
            )
        return updates[-1].response

    def _success(self, endpoint: LLMEndpointModel, response: LLMResponseIR) -> LLMGenerationResult:
        self.last_endpoint_id = endpoint.endpoint_id
        self.last_model_id = endpoint.model_id
        self._record_success(endpoint, response)
        return LLMGenerationResult(
            response=response,
            preferred_endpoint_id=endpoint.endpoint_id,
            preferred_model_id=endpoint.model_id,
        )

    def _record_success(self, endpoint: LLMEndpointModel, response: LLMResponseIR) -> None:
        if response.finish_reason == LLMFinishReason.ERROR:
            raise LLMEndpointResponseError(
                f"endpoint {endpoint.endpoint_id} returned finish_reason=error"
            )
        self.usage_ledger.record_success(
            endpoint_id=endpoint.endpoint_id,
            model_id=endpoint.model_id,
            provider=endpoint.provider,
            usage=response.usage,
            provider_response_count=response.provider_response_count,
        )

    def _record_failure(self, endpoint: LLMEndpointModel, exc: Exception, attempt: int) -> str:
        error_kind = _classify_retry_error(exc)
        self.usage_ledger.record_failed_attempt(
            endpoint_id=endpoint.endpoint_id,
            model_id=endpoint.model_id,
            provider=endpoint.provider,
        )
        self._emit(
            "llm_endpoint_attempt_failed",
            endpoint=endpoint,
            attempt=attempt + 1,
            error_kind=error_kind,
            error_type=type(exc).__name__,
        )
        return error_kind

    def _timeout_seconds(self, request: LLMRequestIR) -> float:
        value = request.metadata.get("timeout_seconds")
        if value is None:
            purpose = str(request.metadata.get("purpose") or "").lower()
            setting = "llm_compaction_timeout_seconds" if "compact" in purpose else "llm_request_timeout_seconds"
            value = getattr(self.config, setting, _DEFAULT_TIMEOUT_SECONDS)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return _DEFAULT_TIMEOUT_SECONDS

    def _stream_wall_timeout_seconds(self, request: LLMRequestIR) -> float:
        value = request.metadata.get("stream_wall_timeout_seconds")
        if value is None:
            value = getattr(
                self.config,
                "llm_stream_wall_timeout_seconds",
                1_800.0,
            )
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return 1_800.0

    def _stream_cleanup_timeout_seconds(self) -> float:
        value = getattr(
            self.config,
            "llm_stream_cleanup_timeout_seconds",
            2.0,
        )
        try:
            return max(0.01, float(value))
        except (TypeError, ValueError):
            return 2.0

    def _emit(self, phase: str, *, endpoint: LLMEndpointModel, **payload: Any) -> None:
        event = {"phase": phase, "endpoint_id": endpoint.endpoint_id, "model_id": endpoint.model_id, **payload}
        if callable(self.event_sink):
            self.event_sink(dict(event))

    def _invoker(self) -> LLMEndpointInvokerPort:
        if self.endpoint_invoker is None:
            raise LLMEndpointInvocationError("LLM endpoint invoker is not configured")
        return self.endpoint_invoker


def _text_response(
    text: str,
    reason: LLMFinishReason,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> LLMResponseIR:
    return LLMResponseIR(
        message=LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR(str(text)),) if str(text) else (),
            state=MessageState.COMPLETE,
            metadata=dict(metadata or {}),
        ),
        finish_reason=reason,
        provider_response_count=0 if reason in {LLMFinishReason.ERROR, LLMFinishReason.COMPACT_REQUIRED} else 1,
    )


def _failure_result(
    text: str,
    *,
    exc: Exception | None = None,
) -> LLMGenerationResult:
    return LLMGenerationResult(
        response=_text_response(
            text,
            LLMFinishReason.ERROR,
            metadata=_failure_metadata(exc),
        )
    )


def _response_with_failure(
    response: LLMResponseIR,
    exc: Exception,
    *,
    partial_output_chars: int = 0,
) -> LLMResponseIR:
    metadata = dict(response.message.metadata)
    metadata.update(_failure_metadata(exc))
    if partial_output_chars > 0:
        metadata["partial_output_chars"] = int(partial_output_chars)
    return replace(
        response,
        message=replace(response.message, metadata=metadata),
        finish_reason=LLMFinishReason.ERROR,
    )


def _failure_metadata(exc: Exception | None) -> dict[str, str]:
    error_kind = _classify_retry_error(exc) if exc is not None else "unknown"
    return {
        "failure_subsystem": (
            "persistence" if error_kind == "local_state" else "llm"
        ),
        "failure_kind": error_kind,
        "error_type": type(exc).__name__ if exc is not None else "UnknownError",
    }


def _classify_retry_error(exc: Exception) -> str:
    if any(isinstance(item, sqlite3.DatabaseError) for item in _exception_chain(exc)):
        return "local_state"
    if isinstance(exc, LLMEndpointSpecStaleError):
        return "endpoint_spec_stale"
    if isinstance(exc, LLMProviderStartedError):
        return "provider_started"
    if isinstance(exc, LLMCredentialUnavailableError):
        return "credential"
    if isinstance(exc, LLMRequestPreparationError):
        return "request"
    if isinstance(exc, LLMEndpointResponseError):
        return "response_error"
    if isinstance(exc, ProviderResponseHookError):
        return "response_error"
    message = str(exc).lower()
    error_type = type(exc).__name__.lower()
    if any(
        marker in message
        for marker in (
            "error code: 401",
            "status code: 401",
            "http 401",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "error code: 403",
            "status code: 403",
            "http 403",
            "forbidden",
        )
    ):
        return "credential"
    if "timeout" in message or "timed out" in message or "timeout" in error_type:
        return "timeout"
    if any(marker in message for marker in ("error code: 400", "status code: 400", "bad request")):
        return "bad_request"
    if any(marker in message for marker in ("connection refused", "connection reset", "broken pipe")) or "connection" in error_type:
        return "connection"
    if "429" in message or "rate limit" in message:
        return "rate_limit"
    if any(marker in message for marker in ("500", "502", "503", "504", "529", "overload")):
        return "server"
    return "unknown"


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _retryable_error_kind(error_kind: str) -> bool:
    return error_kind in {
        "timeout",
        "connection",
        "rate_limit",
        "server",
        "response_error",
        "unknown",
    }


def _public_failure_text(exc: Exception | None) -> str:
    if exc is None:
        return "LLM invocation failed: kind=unknown type=UnknownError"
    return (
        "LLM invocation failed: "
        f"kind={_classify_retry_error(exc)} type={type(exc).__name__}"
    )


def _estimate_request_tokens(request: LLMRequestIR) -> int:
    chars = 0
    for message in request.messages:
        chars += len(message.text) + len(message.reasoning_text)
        for call in message.tool_calls:
            chars += len(call.name) + len(json.dumps(thaw_json(call.arguments), ensure_ascii=False))
    for tool in request.tools:
        chars += len(tool.name) + len(tool.description) + len(json.dumps(thaw_json(tool.input_schema), ensure_ascii=False))
    return max(1, (chars + 3) // 4)


def _request_has_image_input(request: LLMRequestIR) -> bool:
    return any(
        isinstance(part, ImagePartIR)
        for message in request.messages
        for part in message.parts
    )


def _retry_delay(attempt: int) -> float:
    base = min(0.5 * (2 ** max(0, attempt - 1)), 32.0)
    return base + random.random() * base * 0.25


def _is_stub_endpoint(endpoint: LLMEndpointModel) -> bool:
    capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
    return bool(
        capabilities.get("stub")
        or str(endpoint.base_url).startswith("stub://")
    )
