from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from pal.llm.credentials import LiteLLMCredentialResolver

from pal.llm.contracts import (
    CanonicalLLMOutcome,
    CanonicalLLMRequest,
    CanonicalToolCall,
    LLMPreflightAdvice,
    LLMPreflightRequest,
    LLMRuntimePort,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.repository import DEFAULT_THINK_LEVEL, LLMEndpointRepository, RuntimeSettingRepository
from pal.shared import LLMFinishReason, LLMPreflightStatus, LLMStreamEventKind
from pal.stream_events import NormalizedLLMStreamEvent


# ---------------------------------------------------------------------------
# Retry helpers — inspired by Claude Code's withRetry.ts
# ---------------------------------------------------------------------------

_DEFAULT_BASE_RETRY_DELAY_MS = 500
_DEFAULT_MAX_RETRY_DELAY_MS = 32_000
_DEFAULT_STALE_CONNECTION_SETTLE_MS = 300


def _is_stale_connection(message: str) -> bool:
    """Detect keep-alive socket deaths: ECONNRESET, EPIPE, server disconnected.

    These happen when the server closed the TCP connection but the local
    connection pool still holds the dead socket.  The next request picks
    up the stale socket and fails instantly — even to a *different* endpoint
    if they share the same connection pool.

    Claude Code handles this by disabling keep-alive and forcing a new
    client.  We mark the error so the caller can take evasive action.
    """
    return any(
        marker in message
        for marker in (
            "econnreset",
            "epipe",
            "broken pipe",
            "server disconnected",
            "remoteprotocolerror",
        )
    )


def _classify_retry_error(exc: Exception) -> str:
    """Classify an LLM invocation error into a retry category.

    Returns one of: 'stale_connection', 'connection', 'rate_limit', 'server', 'unknown'.

    'stale_connection' is a subset of 'connection' — it specifically means a
    keep-alive socket was reused after the server closed it.  These warrant
    immediate endpoint skip AND a brief settle delay.
    """
    message = str(exc).lower()
    error_type = type(exc).__name__.lower()
    print(f"[llm retry] error_type={error_type} message={message[:200]}")

    if _is_stale_connection(message):
        return "stale_connection"

    # Network / connection level: timeouts, connection refused, etc.
    if any(
        marker in message
        for marker in (
            "timed out",
            "timeout",
            "connectionerror",
            "connection refused",
            "connection aborted",
            "connectionreset",
        )
    ) or any(
        marker in error_type
        for marker in ("connection", "timeout", "protocol")
    ):
        return "connection"

    # HTTP status codes embedded in the error message
    if "429" in message or "rate" in message:
        return "rate_limit"
    if any(code in message for code in ("529", "overload", "502", "503", "504", "500")):
        return "server"

    return "unknown"


def _compute_retry_delay(
    attempt: int,
    *,
    error_kind: str,
    base_delay_ms: int = _DEFAULT_BASE_RETRY_DELAY_MS,
    max_delay_ms: int = _DEFAULT_MAX_RETRY_DELAY_MS,
    stale_settle_ms: int = _DEFAULT_STALE_CONNECTION_SETTLE_MS,
) -> float:
    """Return delay in seconds before the next retry attempt.

    Exponential backoff with jitter, capped at max_delay_ms.
    - stale_connection: brief settle pause only (TCP cleanup)
    - connection: fast skip — just jitter to avoid thundering herd
    - rate_limit / server: full exponential backoff
    """
    if error_kind == "stale_connection":
        return stale_settle_ms / 1000.0

    base = min(base_delay_ms * (2 ** (attempt - 1)), max_delay_ms)
    jitter = random.random() * 0.25 * base  # noqa: S311
    return (base + jitter) / 1000.0


@dataclass
class _EndpointInvocationResult:
    """Carries the outcome of endpoint iteration with retry."""
    kind: str  # "success" | "compact_required" | "error" | "no_endpoints"
    value: Any = None
    endpoint: LLMEndpointModel | None = None
    effective_request: CanonicalLLMRequest | None = None
    target_input_budget: int | None = None
    reserved_output_tokens: int | None = None
    error_message: str = ""


@dataclass
class EndpointResolver:
    repository: LLMEndpointRepository | None = None
    endpoints: tuple[LLMEndpointModel, ...] = ()

    def __post_init__(self) -> None:
        if self.endpoints:
            self.endpoints = tuple(self.endpoints)
            return
        if self.repository is None:
            self.endpoints = ()
            return
        # LLM endpoint topology is loaded once during bootstrap. Changes are
        # picked up by restarting from supervisor-owned provisioning.
        self.endpoints = tuple(self.repository.list_enabled())

    def primary(self, *, preferred_endpoint_id: str | None = None) -> LLMEndpointModel | None:
        enabled = self.enabled(preferred_endpoint_id=preferred_endpoint_id)
        return enabled[0] if enabled else None

    def enabled(self, *, preferred_endpoint_id: str | None = None) -> list[LLMEndpointModel]:
        items = list(self.endpoints)
        if not preferred_endpoint_id:
            return items
        for index, item in enumerate(items):
            if item.endpoint_id == preferred_endpoint_id:
                return [item, *items[:index], *items[index + 1 :]]
        return items


class LLMEndpointInvokerPort(Protocol):
    def invoke(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        ...

    def invoke_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        ...


class LLMEndpointInvocationError(RuntimeError):
    pass


@dataclass
class LiteLLMEndpointInvoker:
    """Unified invoker using LiteLLM, with stub:// endpoints kept local."""

    credentials: LiteLLMCredentialResolver = field(default_factory=LiteLLMCredentialResolver)

    def invoke(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        if endpoint.provider == "stub" or str(endpoint.base_url).startswith("stub://"):
            return self._invoke_stub(endpoint, request)
        return self._invoke_litellm(endpoint, request)

    def invoke_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        if endpoint.provider == "stub" or str(endpoint.base_url).startswith("stub://"):
            return self._invoke_stub_stream(endpoint, request)
        return self._invoke_litellm_stream(endpoint, request)

    def _invoke_stub(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        content = _last_message_text(request.messages)
        text = content.strip() or "stub response"
        response_mode = _coerce_response_mode(request.metadata.get("response_mode_hint"))
        return CanonicalLLMOutcome(
            text=text,
            reasoning_text="",
            tool_calls=[],
            finish_reason=LLMFinishReason.STOP,
            response_mode=response_mode,
        )

    def _invoke_stub_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        _ = endpoint
        content = _last_message_text(request.messages)
        text = content.strip() or "stub response"
        response_mode = _coerce_response_mode(request.metadata.get("response_mode_hint"))
        midpoint = max(1, len(text) // 2)
        return [
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=text[:midpoint]),
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=text[midpoint:]),
            NormalizedLLMStreamEvent(
                event_kind=LLMStreamEventKind.DONE,
                finish_reason=LLMFinishReason.STOP,
                response_mode=response_mode,
            ),
        ]

    def _invoke_litellm(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        try:
            import litellm  # type: ignore
        except Exception as exc:
            raise LLMEndpointInvocationError("litellm is not installed in the current runtime") from exc

        kwargs, tool_name_aliases = self._build_completion_kwargs(endpoint, request)
        try:
            response = litellm.completion(**kwargs)
        except Exception as exc:
            raise LLMEndpointInvocationError(f"litellm invocation failed for {endpoint.endpoint_id}: {exc}") from exc
        return self._parse_litellm_response(response, tool_name_aliases=tool_name_aliases)

    def _invoke_litellm_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        try:
            import litellm  # type: ignore
        except Exception as exc:
            raise LLMEndpointInvocationError("litellm is not installed in the current runtime") from exc

        kwargs, tool_name_aliases = self._build_completion_kwargs(endpoint, request)
        try:
            stream = litellm.completion(stream=True, **kwargs)
        except Exception as exc:
            raise LLMEndpointInvocationError(f"litellm invocation failed for {endpoint.endpoint_id}: {exc}") from exc
        return self._iter_litellm_stream(stream, tool_name_aliases=tool_name_aliases)

    def _build_completion_kwargs(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        tool_name_aliases = _build_tool_name_aliases(request.tools)
        kwargs: dict[str, Any] = {
            "model": _litellm_model(endpoint.model_id, endpoint.api_mode),
            "messages": _coerce_messages_for_litellm(list(request.messages), tool_name_aliases=tool_name_aliases),
            "timeout": 120,
        }
        if endpoint.base_url and not str(endpoint.base_url).startswith("stub://"):
            kwargs["api_base"] = endpoint.base_url
        api_key = self.credentials.resolve_api_key(endpoint)
        if api_key:
            kwargs["api_key"] = api_key
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        tools = _coerce_tools_for_litellm(request.tools, tool_name_aliases=tool_name_aliases)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        thinking_effort = _think_level_to_effort(request.metadata.get("think_level"))
        if endpoint.api_mode == "anthropic_messages" and thinking_effort:
            thinking = _anthropic_thinking_param(thinking_effort, request.max_output_tokens)
            if thinking:
                kwargs["thinking"] = thinking
        return kwargs, tool_name_aliases

    def _iter_litellm_stream(
        self,
        stream: Iterable[Any],
        *,
        tool_name_aliases: dict[str, str] | None = None,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        for raw_chunk in stream:
            for event in _parse_litellm_stream_chunk(raw_chunk, tool_name_aliases=tool_name_aliases):
                yield event

    def _parse_litellm_response(
        self,
        response: Any,
        *,
        tool_name_aliases: dict[str, str] | None = None,
    ) -> CanonicalLLMOutcome:
        payload = response.model_dump() if hasattr(response, "model_dump") else response.to_dict() if hasattr(response, "to_dict") else response
        choices = list((payload or {}).get("choices") or [])
        if not choices:
            return CanonicalLLMOutcome(text="", reasoning_text="", tool_calls=[], finish_reason=LLMFinishReason.STOP)
        first = choices[0] or {}
        message = first.get("message") or {}
        text = _message_text(message)
        reasoning_text = _message_reasoning_text(message)
        return CanonicalLLMOutcome(
            text=text,
            reasoning_text=reasoning_text,
            tool_calls=_parse_tool_calls(message, tool_name_aliases=tool_name_aliases),
            finish_reason=str(first.get("finish_reason") or LLMFinishReason.STOP),
            response_mode=_coerce_response_mode(((payload or {}).get("metadata") or {}).get("response_mode")),
        )


@dataclass
class LLMRuntime(LLMRuntimePort):
    endpoint_resolver: EndpointResolver
    settings_repository: RuntimeSettingRepository
    endpoint_invoker: LLMEndpointInvokerPort | None = None
    config: Any = None
    safety_margin_tokens: int = 16384
    endpoint_retry_attempts: int = 2
    last_request: CanonicalLLMRequest | None = None
    last_endpoint_id: str | None = None
    last_model_id: str | None = None
    think_level: str = DEFAULT_THINK_LEVEL
    active_endpoint_id: str | None = None

    def __post_init__(self) -> None:
        if self.endpoint_invoker is None:
            self.endpoint_invoker = LiteLLMEndpointInvoker()
        self.refresh_runtime_settings()
        if self.config is not None:
            self.endpoint_retry_attempts = self.config.llm_endpoint_retry_attempts

    @property
    def _retry_base_delay_ms(self) -> int:
        return getattr(self.config, "llm_base_retry_delay_ms", _DEFAULT_BASE_RETRY_DELAY_MS) if self.config else _DEFAULT_BASE_RETRY_DELAY_MS

    @property
    def _retry_max_delay_ms(self) -> int:
        return getattr(self.config, "llm_max_retry_delay_ms", _DEFAULT_MAX_RETRY_DELAY_MS) if self.config else _DEFAULT_MAX_RETRY_DELAY_MS

    @property
    def _retry_stale_settle_ms(self) -> int:
        return getattr(self.config, "llm_stale_connection_settle_ms", _DEFAULT_STALE_CONNECTION_SETTLE_MS) if self.config else _DEFAULT_STALE_CONNECTION_SETTLE_MS

    def refresh_runtime_settings(self) -> None:
        self.think_level = self.settings_repository.get_think_level()
        self.active_endpoint_id = self.settings_repository.get_active_llm_endpoint_id()

    def set_active_endpoint(self, endpoint_id: str) -> str:
        normalized = str(endpoint_id or "").strip()
        self.settings_repository.set_active_llm_endpoint_id(normalized)
        self.active_endpoint_id = normalized or None
        return normalized

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        self.refresh_runtime_settings()
        preferred_endpoint_id = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        preferred_endpoint_id = preferred_endpoint_id or self.active_endpoint_id
        enabled = self.endpoint_resolver.enabled(preferred_endpoint_id=preferred_endpoint_id)
        primary = enabled[0] if enabled else None
        return self._build_preflight_advice(
            endpoint=primary,
            request=request,
            fallback_chain=[endpoint.model_id for endpoint in enabled[1:]],
        )

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return await asyncio.to_thread(self.preflight, request)

    def resolve_endpoint_facts(self, *, preferred_endpoint_id: str | None = None) -> dict[str, Any]:
        self.refresh_runtime_settings()
        normalized_preferred = str(preferred_endpoint_id or "").strip() or None
        normalized_preferred = normalized_preferred or self.active_endpoint_id
        endpoint = self.endpoint_resolver.primary(preferred_endpoint_id=normalized_preferred)
        if endpoint is None:
            return {
                "endpoint_id": normalized_preferred,
                "model_id": None,
                "context_window": None,
                "max_output_tokens": None,
            }
        return {
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "context_window": endpoint.context_window,
            "max_output_tokens": endpoint.max_output_tokens,
        }

    def resolve_max_output_tokens(self, *, preferred_endpoint_id: str | None = None) -> int | None:
        endpoint = self.endpoint_resolver.primary(preferred_endpoint_id=preferred_endpoint_id or self.active_endpoint_id)
        if endpoint is not None and endpoint.max_output_tokens is not None:
            return endpoint.max_output_tokens
        return None

    def _invoke_endpoints_with_retry(
        self,
        request: CanonicalLLMRequest,
        invoke_fn,
    ) -> _EndpointInvocationResult:
        """Shared endpoint iteration with retry, backoff, and stale-connection handling."""
        explicit_preferred_endpoint_id = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        preferred_endpoint_id = explicit_preferred_endpoint_id or self.active_endpoint_id
        enabled = list(self.endpoint_resolver.enabled(preferred_endpoint_id=preferred_endpoint_id))
        if not enabled:
            effective_request = self._build_effective_request(request, endpoint=None)
            self.last_request = effective_request
            self.last_endpoint_id = None
            self.last_model_id = None
            return _EndpointInvocationResult(kind="no_endpoints")

        last_error: Exception | None = None
        had_stale_connection = False
        for endpoint in enabled:
            if had_stale_connection:
                time.sleep(self._retry_stale_settle_ms / 1000.0)
                had_stale_connection = False

            effective_request = self._build_effective_request(request, endpoint=endpoint)
            self.last_request = effective_request
            advice = self._build_preflight_advice(
                endpoint=endpoint,
                request=LLMPreflightRequest(
                    messages=effective_request.messages,
                    max_output_tokens=effective_request.max_output_tokens,
                    model_hint=effective_request.model_hint,
                    metadata=dict(effective_request.metadata),
                ),
                fallback_chain=[],
            )
            if advice.status == LLMPreflightStatus.COMPACT_REQUIRED:
                self.last_endpoint_id = endpoint.endpoint_id
                self.last_model_id = endpoint.model_id
                return _EndpointInvocationResult(
                    kind="compact_required",
                    endpoint=endpoint,
                    effective_request=effective_request,
                    target_input_budget=advice.target_input_budget,
                    reserved_output_tokens=advice.reserved_output_tokens,
                )

            for attempt in range(max(1, self.endpoint_retry_attempts)):
                try:
                    result = invoke_fn(endpoint, effective_request)
                    self.last_request = effective_request
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    if explicit_preferred_endpoint_id is None and endpoint.endpoint_id != self.active_endpoint_id:
                        self.set_active_endpoint(endpoint.endpoint_id)
                    return _EndpointInvocationResult(
                        kind="success",
                        value=result,
                        endpoint=endpoint,
                        effective_request=effective_request,
                    )
                except Exception as exc:
                    last_error = exc
                    error_kind = _classify_retry_error(exc)
                    if error_kind == "stale_connection":
                        had_stale_connection = True
                        break
                    if error_kind == "connection":
                        break
                    if attempt < max(1, self.endpoint_retry_attempts) - 1:
                        delay = _compute_retry_delay(
                            attempt + 1,
                            error_kind=error_kind,
                            base_delay_ms=self._retry_base_delay_ms,
                            max_delay_ms=self._retry_max_delay_ms,
                            stale_settle_ms=self._retry_stale_settle_ms,
                        )
                        time.sleep(delay)

        self.last_endpoint_id = None
        self.last_model_id = None
        reason = str(last_error) if last_error is not None else "unknown endpoint invocation error"
        return _EndpointInvocationResult(kind="error", error_message=reason)

    def generate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        self.refresh_runtime_settings()
        result = self._invoke_endpoints_with_retry(
            request,
            invoke_fn=lambda ep, req: self._require_invoker().invoke(ep, req),
        )
        if result.kind == "success":
            return result.value
        if result.kind == "compact_required":
            ep = result.endpoint
            return CanonicalLLMOutcome(
                text="",
                reasoning_text="",
                tool_calls=[],
                finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                target_input_budget=result.target_input_budget,
                reserved_output_tokens=result.reserved_output_tokens,
                preferred_endpoint_id=ep.endpoint_id,
                preferred_model_id=ep.model_id,
            )
        msg = (
            "LLM generation failed: no enabled endpoints are configured."
            if result.kind == "no_endpoints"
            else f"LLM generation failed after exhausting all configured endpoints: {result.error_message}"
        )
        return CanonicalLLMOutcome(
            text=msg,
            reasoning_text="",
            tool_calls=[],
            finish_reason=LLMFinishReason.ERROR,
        )

    async def agenerate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        return await asyncio.to_thread(self.generate, request)

    def generate_stream(self, request: CanonicalLLMRequest) -> list[NormalizedLLMStreamEvent]:
        self.refresh_runtime_settings()
        result = self._invoke_endpoints_with_retry(
            request,
            invoke_fn=lambda ep, req: list(self._require_invoker().invoke_stream(ep, req)),
        )
        if result.kind == "success":
            return result.value
        if result.kind == "compact_required":
            ep = result.endpoint
            return [
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.COMPACT_REQUIRED,
                    finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                    target_input_budget=result.target_input_budget,
                    reserved_output_tokens=result.reserved_output_tokens,
                    preferred_endpoint_id=ep.endpoint_id,
                    preferred_model_id=ep.model_id,
                )
            ]
        msg = (
            "LLM generation failed: no enabled endpoints are configured."
            if result.kind == "no_endpoints"
            else f"LLM generation failed after exhausting all configured endpoints: {result.error_message}"
        )
        return [
            NormalizedLLMStreamEvent(
                event_kind=LLMStreamEventKind.ERROR,
                error_text=msg,
                finish_reason=LLMFinishReason.ERROR,
            ),
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.ERROR),
        ]

    async def agenerate_stream(self, request: CanonicalLLMRequest) -> list[NormalizedLLMStreamEvent]:
        return await asyncio.to_thread(self.generate_stream, request)

    def summarize_compaction(
        self,
        text: str,
        *,
        max_output_tokens: int = 192,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> str:
        request = CanonicalLLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize the recent conversation into a short, durable working-memory summary. "
                        "Preserve user preferences, commitments, active goals, and factual context. "
                        "Do not include markdown, speaker labels, or commentary."
                    ),
                },
                {"role": "user", "content": text.strip()},
            ],
            max_output_tokens=max_output_tokens,
            model_hint=preferred_model_id,
            temperature=0.2,
            tools=[],
            metadata={
                "preferred_endpoint_id": preferred_endpoint_id,
                "response_mode_hint": "operational",
                "purpose": "memory_compaction",
            },
        )
        outcome = self.generate(request)
        if outcome.finish_reason == LLMFinishReason.COMPACT_REQUIRED:
            return ""
        return str(outcome.text or "").strip()

    async def asummarize_compaction(
        self,
        text: str,
        *,
        max_output_tokens: int = 192,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.summarize_compaction,
            text,
            max_output_tokens=max_output_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_model_id=preferred_model_id,
        )

    _COMPACT_STRUCTURED_SYSTEM = (
        "You are a memory compaction engine.\n"
        "Read the conversation context below and produce a structured JSON object with two keys:\n"
        "\n"
        '  "summary": a JSON object with fields:\n'
        "    - summary (string, required): rolling conversation summary (compressed, for prompt display)\n"
        "    - search_text (string, required): the original verbatim conversation content that this summary covers — source of truth for retrieval\n"
        "\n"
        '  "entries": a list of zero or more extracted items, each with:\n'
        "    - kind (string): \"fact\" or \"case\"\n"
        "    - title (string, required): short label identifying this memory\n"
        "    - summary (string, required): concise summary for future LLM consumption\n"
        "    - search_text (string, required): the original verbatim fact or statement — source of truth, used for retrieval indexing. Do NOT compress or paraphrase.\n"
        "    - canonical_key (string, optional): stable identity key for dedup\n"
        "    - scope (string, optional): \"system\" or \"task\"\n"
        "    - task_id (string, optional)\n"
        "    - payload (object, optional): for case kind, include situation/task/action/result\n"
        "\n"
        "Rules:\n"
        "- Output valid JSON only, no markdown fences.\n"
        "- Extract user preferences, commitments, and domain facts as fact entries.\n"
        "- Extract problem-solving episodes as case entries with situation/task/action/result.\n"
        "- Do not invent information not present in the source.\n"
        "- If nothing worth extracting, return an empty entries list.\n"
        "- summary is always required and should cover the conversation arc.\n"
        "- title, summary, and search_text serve different purposes:\n"
        "  * title: short label for display\n"
        "  * summary: compressed version for prompt consumption\n"
        "  * search_text: original source text for retrieval — must preserve key terms and context\n"
    )

    def compact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> dict[str, Any]:
        request = CanonicalLLMRequest(
            messages=[
                {"role": "system", "content": self._COMPACT_STRUCTURED_SYSTEM},
                {"role": "user", "content": text.strip()},
            ],
            max_output_tokens=max_output_tokens,
            model_hint=preferred_model_id,
            temperature=0.15,
            tools=[],
            metadata={
                "preferred_endpoint_id": preferred_endpoint_id,
                "response_mode_hint": "operational",
                "purpose": "memory_compaction_structured",
            },
        )
        outcome = self.generate(request)
        if outcome.finish_reason == LLMFinishReason.COMPACT_REQUIRED:
            return {}
        raw = str(outcome.text or "").strip()
        return _extract_compaction_json(raw)

    async def acompact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.compact_memory_structured,
            text,
            max_output_tokens=max_output_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_model_id=preferred_model_id,
        )

    def _build_preflight_advice(
        self,
        *,
        endpoint: LLMEndpointModel | None,
        request: LLMPreflightRequest,
        fallback_chain: list[str],
    ) -> LLMPreflightAdvice:
        breakdown = self._build_preflight_breakdown(request)
        reserved_output_tokens = request.max_output_tokens
        if endpoint is not None and endpoint.max_output_tokens is not None:
            reserved_output_tokens = min(request.max_output_tokens, endpoint.max_output_tokens)
        if endpoint is None or endpoint.context_window is None:
            available_input_budget_chars = max(
                int(breakdown.get("estimated_input_chars", 0)),
                int(breakdown.get("hard_keep_chars", 0)),
            )
            target_budget = max(
                256,
                int(breakdown.get("conversation_chars", 0)),
            )
            hard_overflow = False
        else:
            margin_tokens = self._context_margin_tokens(endpoint.context_window)
            available_input_tokens = max(endpoint.context_window - reserved_output_tokens - margin_tokens, 0)
            available_input_budget_chars = int(available_input_tokens * self._chars_per_token())
            remaining_conversation_budget = max(
                available_input_budget_chars - int(breakdown.get("hard_keep_chars", 0)),
                0,
            )
            target_budget = max(256, int(remaining_conversation_budget * 0.5))
            hard_overflow = int(breakdown.get("hard_keep_chars", 0)) > available_input_budget_chars
            breakdown["context_window_tokens"] = int(endpoint.context_window)
            breakdown["margin_tokens"] = int(margin_tokens)
            breakdown["available_input_tokens"] = int(available_input_tokens)
        breakdown["available_input_budget_chars"] = int(available_input_budget_chars)
        breakdown["hard_overflow"] = hard_overflow
        active_model = endpoint.model_id if endpoint is not None else request.model_hint
        estimated_input_chars = int(breakdown.get("estimated_input_chars", 0))
        status = (
            LLMPreflightStatus.COMPACT_REQUIRED
            if estimated_input_chars > available_input_budget_chars
            else LLMPreflightStatus.READY
        )
        return LLMPreflightAdvice(
            status=status,
            active_model=active_model,
            fallback_chain=list(fallback_chain),
            target_input_budget=target_budget,
            reserved_output_tokens=reserved_output_tokens,
            breakdown=breakdown,
        )

    def _build_preflight_breakdown(self, request: LLMPreflightRequest) -> dict[str, int | bool]:
        snapshot = request.metadata.get("prompt_budget_snapshot")
        if isinstance(snapshot, dict):
            breakdown = {
                key: int(snapshot.get(key, 0))
                for key in (
                    "system_chars",
                    "tool_protocol_chars",
                    "conversation_chars",
                    "current_user_chars",
                    "estimated_input_chars",
                    "hard_keep_chars",
                )
            }
            return breakdown
        system_chars = 0
        tool_protocol_chars = 0
        conversation_chars = 0
        current_user_chars = 0
        last_index = len(request.messages) - 1
        for index, message in enumerate(request.messages):
            content_chars = self._estimate_message_chars(message)
            role = str(message.get("role") or "").strip()
            if role == "system":
                system_chars += content_chars
                continue
            if role == "tool" or (role == "assistant" and message.get("tool_calls")):
                tool_protocol_chars += content_chars
                continue
            if role == "user" and index == last_index:
                current_user_chars += content_chars
                continue
            conversation_chars += content_chars
        estimated_input_chars = system_chars + tool_protocol_chars + conversation_chars + current_user_chars
        return {
            "system_chars": system_chars,
            "tool_protocol_chars": tool_protocol_chars,
            "conversation_chars": conversation_chars,
            "current_user_chars": current_user_chars,
            "estimated_input_chars": estimated_input_chars,
            "hard_keep_chars": system_chars + tool_protocol_chars + current_user_chars,
        }

    @staticmethod
    def _estimate_message_chars(message: dict[str, Any]) -> int:
        total = len(str(message.get("content", "")))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            try:
                total += len(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True))
            except TypeError:
                total += len(str(tool_calls))
        tool_call_id = str(message.get("tool_call_id", "") or "").strip()
        if tool_call_id:
            total += len(tool_call_id)
        return total

    def _context_margin_tokens(self, context_window: int) -> int:
        if self.config is None:
            return min(self.safety_margin_tokens, max(1024, int(context_window * 0.05)))
        return min(
            int(getattr(self.config, "context_margin_cap", self.safety_margin_tokens)),
            max(
                int(getattr(self.config, "context_margin_min", 1024)),
                int(context_window * float(getattr(self.config, "context_margin_factor", 0.05))),
            ),
        )

    def _chars_per_token(self) -> float:
        if self.config is None:
            return 3.5
        value = float(getattr(self.config, "chars_per_token", 3.5))
        return value if value > 0 else 3.5

    def _build_effective_request(
        self,
        request: CanonicalLLMRequest,
        *,
        endpoint: LLMEndpointModel | None,
    ) -> CanonicalLLMRequest:
        requested_think_level = str(request.metadata.get("think_level") or "").strip() or self.think_level
        metadata: dict[str, Any] = {
            **dict(request.metadata),
            "think_level": requested_think_level,
        }
        if endpoint is not None:
            metadata.update(
                {
                    "endpoint_id": endpoint.endpoint_id,
                    "provider": endpoint.provider,
                    "model_id": endpoint.model_id,
                    "supports_streaming": bool(endpoint.supports_streaming),
                }
            )
        return CanonicalLLMRequest(
            messages=list(request.messages),
            max_output_tokens=request.max_output_tokens,
            model_hint=request.model_hint or (endpoint.model_id if endpoint is not None else None),
            temperature=request.temperature,
            tools=list(request.tools),
            metadata=metadata,
        )

    def _require_invoker(self) -> LLMEndpointInvokerPort:
        if self.endpoint_invoker is None:
            raise LLMEndpointInvocationError("llm endpoint invoker is not configured")
        return self.endpoint_invoker

def _last_message_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            return content
    return ""


def _coerce_response_mode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text


def _litellm_model(model_id: str, api_mode: str) -> str:
    if "/" in model_id:
        return model_id
    if api_mode == "anthropic_messages":
        return f"anthropic/{model_id}"
    return f"openai/{model_id}"


def _think_level_to_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "off": None,
        "low": "low",
        "balanced": "medium",
        "medium": "medium",
        "deep": "high",
        "high": "high",
    }
    return mapping.get(text, "medium" if text else None)


def _anthropic_thinking_param(effort: str | None, max_output_tokens: int | None) -> dict[str, Any] | None:
    if effort is None:
        return None
    budget_map = {"low": 1024, "medium": 8192, "high": 32768}
    budget = budget_map.get(effort, 8192)
    if max_output_tokens is not None and budget >= max_output_tokens:
        budget = max(1024, max_output_tokens // 2)
    return {"type": "enabled", "budget_tokens": budget}


def _build_tool_name_aliases(tools: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str((function or {}).get("name") or tool.get("name") or "").strip()
        if not name:
            continue
        aliases[name] = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "tool"
    return aliases


def _external_tool_name(name: str, tool_name_aliases: dict[str, str] | None) -> str:
    return str((tool_name_aliases or {}).get(name, name))


def _canonical_tool_name(name: str, tool_name_aliases: dict[str, str] | None) -> str:
    reverse = {alias: canonical for canonical, alias in (tool_name_aliases or {}).items()}
    return str(reverse.get(name, name))


def _coerce_tools_for_litellm(
    tools: list[dict[str, Any]],
    *,
    tool_name_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if "type" in tool and "function" in tool:
            payload = dict(tool)
            function = dict(payload.get("function") or {})
            name = str(function.get("name") or "").strip()
            if name:
                function["name"] = _external_tool_name(name, tool_name_aliases)
            payload["function"] = function
            normalized.append(payload)
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        normalized.append(
            {
                "type": "function",
                "function": {
                    "name": _external_tool_name(name, tool_name_aliases),
                    "description": f"Tool {name}",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
    return normalized


def _coerce_messages_for_litellm(
    messages: list[dict[str, Any]],
    *,
    tool_name_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        payload = dict(message)
        tool_calls = list(payload.get("tool_calls") or [])
        if tool_calls:
            coerced_calls: list[dict[str, Any]] = []
            for item in tool_calls:
                if not isinstance(item, dict):
                    continue
                tool_payload = dict(item)
                function = dict(tool_payload.get("function") or {})
                name = str(function.get("name") or "").strip()
                if name:
                    function["name"] = _external_tool_name(name, tool_name_aliases)
                tool_payload["function"] = function
                coerced_calls.append(tool_payload)
            payload["tool_calls"] = coerced_calls
        normalized.append(payload)
    return normalized


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if text:
                    chunks.append(str(text))
        return "".join(chunks)
    return ""


def _message_reasoning_text(message: dict[str, Any]) -> str:
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning
    provider_fields = message.get("provider_specific_fields")
    if isinstance(provider_fields, dict):
        nested = provider_fields.get("reasoning_content")
        if isinstance(nested, str):
            return nested
    return ""


def _parse_tool_calls(
    message: dict[str, Any],
    *,
    tool_name_aliases: dict[str, str] | None = None,
) -> list[CanonicalToolCall]:
    payload = list(message.get("tool_calls") or [])
    result: list[CanonicalToolCall] = []
    for item in payload:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        raw_args = function.get("arguments") or {}
        args: dict[str, Any]
        if isinstance(raw_args, str):
            import json

            try:
                loaded = json.loads(raw_args)
            except Exception:
                loaded = {}
            args = dict(loaded or {})
        elif isinstance(raw_args, dict):
            args = dict(raw_args)
        else:
            args = {}
        if name:
            result.append(
                CanonicalToolCall(
                    name=_canonical_tool_name(name, tool_name_aliases),
                    args=args,
                    call_id=str(item.get("id") or "").strip() or None,
                )
            )
    return result


def _parse_litellm_stream_chunk(
    raw_chunk: Any,
    *,
    tool_name_aliases: dict[str, str] | None = None,
) -> list[NormalizedLLMStreamEvent]:
    payload = raw_chunk.model_dump() if hasattr(raw_chunk, "model_dump") else raw_chunk.to_dict() if hasattr(raw_chunk, "to_dict") else raw_chunk
    choices = list((payload or {}).get("choices") or [])
    if not choices:
        return []
    first = choices[0] or {}
    delta = first.get("delta") or {}
    finish_reason = first.get("finish_reason")
    events: list[NormalizedLLMStreamEvent] = []
    content = delta.get("content")
    reasoning_content = delta.get("reasoning_content")
    if isinstance(content, str):
        if content:
            events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=content))
    elif isinstance(content, list):
        text = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        if text:
            events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=text))
    if isinstance(reasoning_content, str):
        if reasoning_content:
            events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.REASONING_DELTA, reasoning_text=reasoning_content))
    for tool_call in _parse_tool_calls(delta if isinstance(delta, dict) else {}, tool_name_aliases=tool_name_aliases):
        events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TOOL_CALL, tool_call=tool_call))
    if finish_reason is not None:
        events.append(
            NormalizedLLMStreamEvent(
                event_kind=LLMStreamEventKind.DONE,
                finish_reason=str(finish_reason),
            )
        )
    return events


def _extract_compaction_json(raw: str) -> dict[str, Any]:
    # Strip markdown code fences if present
    stripped = raw.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n") if "\n" in stripped else len(stripped)
        stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    if "summary" not in parsed:
        return {}
    return parsed
