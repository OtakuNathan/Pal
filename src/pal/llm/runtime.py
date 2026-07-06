from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from uuid import uuid4

from pal.llm.adapters import LLMProviderRegistry, build_runtime_provider_registry, _think_level_to_completion_reasoning_effort
from pal.llm.llm_adaptor.base import OPENAI_RESPONSES_SHAPE
from pal.llm.llm_adaptor.anthropic_api import (
    chat_messages_to_anthropic_messages,
    chat_tools_to_anthropic_tools,
    think_level_to_anthropic_thinking,
)
from pal.llm.llm_adaptor.openai_responses import OpenAIResponsesDraft, chat_tools_to_responses_tools
from pal.llm.codex_openai_bridge import (
    DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY,
    CodexCliBridge,
    CodexCompletion,
    CodexBridgeError,
    _messages_to_codex_input,
    _openai_tools_to_dynamic_tools,
    _strip_openai_prefix,
)
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.request_hooks import apply_llm_message_hooks, is_zai_glm_endpoint

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
from pal.memory.contracts import CompactionProfile
from pal.shared import LLMFinishReason, LLMPreflightStatus, LLMStreamEventKind, llm_tool_name
from pal.stream_events import NormalizedLLMStreamEvent


# ---------------------------------------------------------------------------
# Retry helpers — inspired by Claude Code's withRetry.ts
# ---------------------------------------------------------------------------

_DEFAULT_BASE_RETRY_DELAY_MS = 500
_DEFAULT_MAX_RETRY_DELAY_MS = 32_000
_DEFAULT_STALE_CONNECTION_SETTLE_MS = 300
_DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 180.0
_DEFAULT_LLM_COMPACTION_TIMEOUT_SECONDS = 180.0
_ENDPOINT_FALLBACK_DISABLED_POLICIES = {
    "disabled",
    "none",
    "off",
    "strict",
    "strict_preferred",
    "no_fallback",
}
_STRICT_ENDPOINT_PREFERRED_SOURCES = {"profile"}
_AUDIT_REDACTED_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "credential",
    "credential_ref",
    "password",
    "secret",
    "token",
}
_AUDIT_MAX_STRING_CHARS = 48_000
_SCOPED_LLM_EVENT_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "pal_llm_event_sink",
    default=None,
)


@contextmanager
def scoped_llm_event_sink(sink: Callable[[dict[str, Any]], None] | None):
    token = _SCOPED_LLM_EVENT_SINK.set(sink if callable(sink) else None)
    try:
        yield
    finally:
        _SCOPED_LLM_EVENT_SINK.reset(token)


def _endpoint_fallback_disabled(policy: Any) -> bool:
    return str(policy or "").strip().lower() in _ENDPOINT_FALLBACK_DISABLED_POLICIES


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

    Returns one of: 'stale_connection', 'timeout', 'connection', 'rate_limit', 'server', 'unknown'.

    'stale_connection' is a subset of 'connection' — it specifically means a
    keep-alive socket was reused after the server closed it.  These warrant
    immediate endpoint skip AND a brief settle delay.
    """
    message = str(exc).lower()
    error_type = type(exc).__name__.lower()
    print(f"[llm retry] error_type={error_type} message={message[:200]}")

    if _is_stale_connection(message):
        return "stale_connection"

    if (
        any(marker in message for marker in ("timed out", "timeout"))
        or "timeout" in error_type
    ):
        return "timeout"

    # Network / connection level: connection refused, protocol reset, etc.
    if any(
        marker in message
        for marker in (
            "connectionerror",
            "connection refused",
            "connection aborted",
            "connectionreset",
        )
    ) or any(
        marker in error_type
        for marker in ("connection", "protocol")
    ):
        return "connection"

    # HTTP status codes embedded in the error message
    if "429" in message or "rate" in message:
        return "rate_limit"
    if any(code in message for code in ("529", "overload", "502", "503", "504", "500", "internalservererror", "network error")):
        return "server"

    return "unknown"


def _endpoint_connection_failure_domain(endpoint: LLMEndpointModel) -> tuple[str, str] | None:
    provider = str(getattr(endpoint, "provider", "") or "").strip().lower()
    base_url = str(getattr(endpoint, "base_url", "") or "").strip().lower()
    capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
    if (
        provider in {"codex", "codex_cli"}
        or base_url.startswith("codex://")
        or capabilities.get("codex_cli")
        or capabilities.get("official_codex_cli")
        or capabilities.get("codex_native")
    ):
        return ("codex_cli", base_url or "codex://cli")
    return None


def _next_endpoint_id(endpoints: list[LLMEndpointModel], index: int) -> str:
    next_index = int(index) + 1
    if next_index < len(endpoints):
        return str(endpoints[next_index].endpoint_id or "")
    return ""


def _short_error_text(exc: Exception, limit: int = 500) -> str:
    text = " ".join(str(exc or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


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


def _coerce_timeout_seconds(value: Any, *, default: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = default
    if seconds <= 0:
        seconds = default
    return max(1.0, seconds)


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
        self.refresh()

    def refresh(self) -> tuple[LLMEndpointModel, ...]:
        if self.repository is None:
            self.endpoints = tuple(self.endpoints)
        else:
            self.endpoints = tuple(self.repository.list_enabled())
        return self.endpoints

    def primary(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        fallback_endpoint_id: str | None = None,
    ) -> LLMEndpointModel | None:
        enabled = self.enabled(preferred_endpoint_id=preferred_endpoint_id, fallback_endpoint_id=fallback_endpoint_id)
        return enabled[0] if enabled else None

    def enabled(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        fallback_endpoint_id: str | None = None,
        include_remaining: bool = True,
    ) -> list[LLMEndpointModel]:
        items = list(self.endpoints)
        preferred = str(preferred_endpoint_id or "").strip()
        fallback = str(fallback_endpoint_id or "").strip()
        if not preferred and not fallback:
            return items if include_remaining else items[:1]
        ordered: list[LLMEndpointModel] = []
        seen: set[str] = set()
        for endpoint_id in (preferred, fallback):
            if not endpoint_id or endpoint_id in seen:
                continue
            match = next((item for item in items if item.endpoint_id == endpoint_id), None)
            if match is None:
                continue
            ordered.append(match)
            seen.add(endpoint_id)
        if include_remaining:
            ordered.extend(item for item in items if item.endpoint_id not in seen)
        return ordered


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


def _timeout_from_openai_kwargs(kwargs: dict[str, Any]) -> float:
    for key in ("force_timeout", "request_timeout", "timeout"):
        value = kwargs.get(key)
        if value is None:
            continue
        return _coerce_timeout_seconds(value, default=120.0)
    return 120.0


def _run_with_wall_timeout(
    operation: Callable[[], Any],
    *,
    timeout_seconds: float,
    description: str,
) -> Any:
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            result_queue.put(("ok", operation()))
        except BaseException as exc:  # noqa: BLE001
            result_queue.put(("error", exc))

    thread = threading.Thread(target=target, name="pal-llm-call", daemon=True)
    thread.start()
    thread.join(timeout=max(0.001, float(timeout_seconds)))
    if thread.is_alive():
        raise LLMEndpointInvocationError(f"{description} timed out after {timeout_seconds:g}s")
    try:
        kind, payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise LLMEndpointInvocationError(f"{description} ended without returning a result") from exc
    if kind == "error":
        raise payload
    return payload


def _run_llm_with_wall_timeout(
    operation: Callable[[], Any],
    *,
    timeout_seconds: float,
    description: str,
) -> Any:
    return _run_with_wall_timeout(operation, timeout_seconds=timeout_seconds, description=description)


@dataclass
class OpenAIChatEndpointInvoker:
    """OpenAI-compatible chat invoker, with stub:// endpoints kept local."""

    credentials: LLMCredentialResolver = field(default_factory=LLMCredentialResolver)
    artifact_manager: Any = None
    runtime_root: str | Path | None = None
    provider_registry: LLMProviderRegistry = field(default_factory=build_runtime_provider_registry)
    message_hooks: tuple[str, ...] = ()
    last_payload_summary: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.runtime_root is not None:
            self.provider_registry.load_runtime_adapters(self.runtime_root)

    def refresh_credentials(self) -> bool:
        refresh = getattr(self.credentials, "refresh", None)
        if callable(refresh):
            refresh()
            return True
        clear_cache = getattr(self.credentials, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()
            return True
        return False

    def refresh_provider_registry(self) -> bool:
        self.provider_registry.refresh_external_sources(runtime_root=self.runtime_root)
        return True

    def invoke(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        if endpoint.provider == "stub" or str(endpoint.base_url).startswith("stub://"):
            self.last_payload_summary = _summarize_provider_payload(endpoint, request.messages, image_url_format="stub")
            return self._invoke_stub(endpoint, request)
        return self._invoke_openai_chat(endpoint, request)

    def invoke_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        if endpoint.provider == "stub" or str(endpoint.base_url).startswith("stub://"):
            self.last_payload_summary = _summarize_provider_payload(endpoint, request.messages, image_url_format="stub")
            return self._invoke_stub_stream(endpoint, request)
        return self._invoke_openai_chat_stream(endpoint, request)

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

    def _invoke_openai_chat(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> CanonicalLLMOutcome:
        try:
            import openai  # type: ignore
        except Exception as exc:
            raise LLMEndpointInvocationError("openai SDK is not installed in the current runtime") from exc

        request_shape, kwargs, tool_name_aliases = self._build_openai_request_kwargs(endpoint, request)
        try:
            if request_shape == OPENAI_RESPONSES_SHAPE:
                client_kwargs, request_kwargs = _split_openai_responses_sdk_kwargs(kwargs)
                client = openai.OpenAI(**client_kwargs)
                response = _run_llm_with_wall_timeout(
                    lambda: client.responses.create(**request_kwargs),
                    timeout_seconds=_timeout_from_openai_kwargs(kwargs),
                    description=f"openai responses invocation for {endpoint.endpoint_id}",
                )
                return self._parse_openai_responses_response(response, tool_name_aliases=tool_name_aliases)
            client_kwargs, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
            client = openai.OpenAI(**client_kwargs)
            response = _run_llm_with_wall_timeout(
                lambda: client.chat.completions.create(**request_kwargs),
                timeout_seconds=_timeout_from_openai_kwargs(kwargs),
                description=f"openai chat invocation for {endpoint.endpoint_id}",
            )
        except Exception as exc:
            raise LLMEndpointInvocationError(f"openai chat invocation failed for {endpoint.endpoint_id}: {exc}") from exc
        return self._parse_openai_chat_response(response, tool_name_aliases=tool_name_aliases)

    def _invoke_openai_chat_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        try:
            import openai  # type: ignore
        except Exception as exc:
            raise LLMEndpointInvocationError("openai SDK is not installed in the current runtime") from exc

        request_shape, kwargs, tool_name_aliases = self._build_openai_request_kwargs(endpoint, request)
        if request_shape == OPENAI_RESPONSES_SHAPE:
            outcome = self._invoke_openai_chat(endpoint, request)
            events: list[NormalizedLLMStreamEvent] = []
            for tool_call in outcome.tool_calls:
                events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TOOL_CALL, tool_call=tool_call))
            if outcome.tool_calls:
                events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.TOOL_CALLS))
                return events
            if outcome.text:
                events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=outcome.text))
            events.append(NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=outcome.finish_reason))
            return events
        try:
            client_kwargs, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
            request_kwargs["stream"] = True
            client = openai.OpenAI(**client_kwargs)
            return _run_llm_with_wall_timeout(
                lambda: list(
                    self._iter_openai_chat_stream(
                        client.chat.completions.create(**request_kwargs),
                        tool_name_aliases=tool_name_aliases,
                    )
                ),
                timeout_seconds=_timeout_from_openai_kwargs(kwargs),
                description=f"openai chat streaming invocation for {endpoint.endpoint_id}",
            )
        except Exception as exc:
            raise LLMEndpointInvocationError(f"openai chat invocation failed for {endpoint.endpoint_id}: {exc}") from exc

    def _build_completion_kwargs(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        _, kwargs, tool_name_aliases = self._build_openai_request_kwargs(endpoint, request)
        return kwargs, tool_name_aliases

    def _build_openai_request_kwargs(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        tool_name_aliases = _build_tool_name_aliases(request.tools)
        image_url_format = _image_url_format(endpoint)
        messages = _coerce_messages_for_openai_chat(
            list(request.messages),
            tool_name_aliases=tool_name_aliases,
            artifact_manager=self.artifact_manager,
            supports_vision=bool(endpoint.supports_vision),
            image_url_format=image_url_format,
        )
        adapter = self.provider_registry.resolve(endpoint)
        request_shape = str(getattr(adapter, "request_shape", "") or "").strip() or "chat_completions"
        draft = adapter.new_draft(messages)
        timeout_seconds = request.metadata.get("timeout_seconds")
        if timeout_seconds is not None:
            try:
                timeout_value = max(1, int(float(timeout_seconds)))
                draft.timeout = timeout_value
                if hasattr(draft, "request_timeout"):
                    draft.request_timeout = float(timeout_value)
                if hasattr(draft, "force_timeout"):
                    draft.force_timeout = float(timeout_value)
            except (TypeError, ValueError):
                pass
        if endpoint.base_url and not str(endpoint.base_url).startswith("stub://"):
            draft.api_base = _openai_api_base(str(endpoint.base_url))
        api_key = self.credentials.resolve_api_key(endpoint)
        if api_key:
            draft.api_key = api_key
        elif endpoint.auth_kind == "local_provider_auth" and endpoint.api_mode == "openai_chat":
            # The OpenAI SDK requires a non-empty key even for local
            # OpenAI-compatible endpoints that ignore Authorization.
            draft.api_key = "local-provider-auth"
        if request.temperature is not None:
            draft.temperature = request.temperature
        if request.max_output_tokens is not None:
            if isinstance(draft, OpenAIResponsesDraft):
                draft.max_output_tokens = request.max_output_tokens
            else:
                draft.max_tokens = request.max_output_tokens
        if isinstance(draft, OpenAIResponsesDraft):
            tools = chat_tools_to_responses_tools(
                _coerce_tools_for_openai_chat(request.tools, tool_name_aliases=tool_name_aliases)
            )
        else:
            tools = _coerce_tools_for_openai_chat(request.tools, tool_name_aliases=tool_name_aliases)
        if tools:
            draft.tools = tools
            draft.tool_choice = "auto"
        adapter.apply_request(request, draft)
        if is_zai_glm_endpoint(endpoint) and self.message_hooks and hasattr(draft, "messages"):
            draft.messages = apply_llm_message_hooks(
                endpoint,
                request,
                list(draft.messages or []),
                hooks=self.message_hooks,
            )
        if hasattr(draft, "messages"):
            messages = list(draft.messages or [])
        self.last_payload_summary = _summarize_provider_payload(endpoint, messages, image_url_format=image_url_format)
        return request_shape, draft.to_kwargs(), tool_name_aliases

    def _iter_openai_chat_stream(
        self,
        stream: Iterable[Any],
        *,
        tool_name_aliases: dict[str, str] | None = None,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        for raw_chunk in stream:
            for event in _parse_openai_chat_stream_chunk(raw_chunk, tool_name_aliases=tool_name_aliases):
                yield event

    def _parse_openai_chat_response(
        self,
        response: Any,
        *,
        tool_name_aliases: dict[str, str] | None = None,
    ) -> CanonicalLLMOutcome:
        payload = response.model_dump() if hasattr(response, "model_dump") else response.to_dict() if hasattr(response, "to_dict") else response
        choices = list((payload or {}).get("choices") or [])
        if not choices:
            raise LLMEndpointInvocationError("llm response contained no choices")
        first = choices[0] or {}
        message = first.get("message") or {}
        text = _message_text(message)
        reasoning_text = _message_reasoning_text(message)
        outcome = CanonicalLLMOutcome(
            text=text,
            reasoning_text=reasoning_text,
            provider_specific_fields=_message_provider_specific_fields(message),
            tool_calls=_parse_tool_calls(message, tool_name_aliases=tool_name_aliases),
            finish_reason=str(first.get("finish_reason") or LLMFinishReason.STOP),
            response_mode=_coerce_response_mode(((payload or {}).get("metadata") or {}).get("response_mode")),
        )
        _ensure_llm_invocation_result_has_payload(outcome)
        return outcome

    def _parse_openai_responses_response(
        self,
        response: Any,
        *,
        tool_name_aliases: dict[str, str] | None = None,
    ) -> CanonicalLLMOutcome:
        payload = response.model_dump() if hasattr(response, "model_dump") else response.to_dict() if hasattr(response, "to_dict") else response
        if isinstance(payload, dict) and payload.get("choices"):
            return self._parse_openai_chat_response(payload, tool_name_aliases=tool_name_aliases)
        output_items = list((payload or {}).get("output") or []) if isinstance(payload, dict) else []
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[CanonicalToolCall] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            if item_type == "message":
                for content in list(item.get("content") or []):
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") == "output_text":
                        text = str(content.get("text") or "")
                        if text:
                            text_parts.append(text)
                continue
            if item_type == "function_call":
                name = _canonical_tool_name(str(item.get("name") or ""), tool_name_aliases)
                args = _coerce_tool_args(item.get("arguments"))
                if name:
                    tool_calls.append(
                        CanonicalToolCall(
                            name=name,
                            args=args,
                            call_id=str(item.get("call_id") or item.get("id") or "") or None,
                        )
                    )
                continue
            if item_type == "reasoning":
                for summary in list(item.get("summary") or []):
                    if isinstance(summary, dict):
                        text = str(summary.get("text") or "")
                        if text:
                            reasoning_parts.append(text)
        outcome = CanonicalLLMOutcome(
            text="".join(text_parts),
            reasoning_text="\n".join(reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=LLMFinishReason.TOOL_CALLS if tool_calls else LLMFinishReason.STOP,
            response_mode=_coerce_response_mode(((payload or {}).get("metadata") or {}).get("response_mode")) if isinstance(payload, dict) else None,
        )
        _ensure_llm_invocation_result_has_payload(outcome)
        return outcome


def _ensure_llm_invocation_result_has_payload(result: Any) -> None:
    if isinstance(result, CanonicalLLMOutcome):
        if _is_empty_successful_llm_outcome(result):
            raise LLMEndpointInvocationError("llm response contained no assistant content or tool calls")
        return
    if isinstance(result, list) and all(isinstance(item, NormalizedLLMStreamEvent) for item in result):
        if _is_empty_successful_stream_result(result):
            raise LLMEndpointInvocationError("llm stream ended without assistant content or tool calls")


def _is_empty_successful_llm_outcome(outcome: CanonicalLLMOutcome) -> bool:
    finish_reason = str(outcome.finish_reason or "").strip()
    if finish_reason in {LLMFinishReason.ERROR, LLMFinishReason.COMPACT_REQUIRED}:
        return False
    return not str(outcome.text or "").strip() and not list(outcome.tool_calls or [])


def _is_empty_successful_stream_result(events: list[NormalizedLLMStreamEvent]) -> bool:
    saw_terminal_error = False
    saw_payload = False
    for event in events:
        if event.event_kind in {LLMStreamEventKind.ERROR, LLMStreamEventKind.COMPACT_REQUIRED}:
            saw_terminal_error = True
        elif event.event_kind == LLMStreamEventKind.TEXT_DELTA and str(event.text or "").strip():
            saw_payload = True
        elif event.event_kind == LLMStreamEventKind.TOOL_CALL and event.tool_call is not None:
            saw_payload = True
    return not saw_terminal_error and not saw_payload


@dataclass
class OpenAIResponsesEndpointInvoker:
    credentials: LLMCredentialResolver = field(default_factory=LLMCredentialResolver)
    artifact_manager: Any = None
    runtime_root: str | Path | None = None
    provider_registry: LLMProviderRegistry = field(default_factory=build_runtime_provider_registry)
    message_hooks: tuple[str, ...] = ()
    _renderer: OpenAIChatEndpointInvoker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._renderer = OpenAIChatEndpointInvoker(
            credentials=self.credentials,
            artifact_manager=self.artifact_manager,
            runtime_root=self.runtime_root,
            provider_registry=self.provider_registry,
            message_hooks=self.message_hooks,
        )

    @staticmethod
    def supports_endpoint(endpoint: LLMEndpointModel) -> bool:
        capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
        provider = str(getattr(endpoint, "provider", "") or "").strip().lower()
        adapter = str(capabilities.get("adapter") or capabilities.get("llm_adapter") or "").strip().lower()
        return bool(
            provider in {"openai", "codex_bridge"}
            or adapter in {"openai_responses", "responses", "codex_bridge"}
            or capabilities.get("openai_responses")
            or capabilities.get("responses_api")
            or capabilities.get("codex_bridge")
        )

    def refresh_credentials(self) -> bool:
        return self._renderer.refresh_credentials()

    def refresh_provider_registry(self) -> bool:
        return self._renderer.refresh_provider_registry()

    def invoke(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        try:
            import openai  # type: ignore
        except Exception as exc:
            raise LLMEndpointInvocationError("openai SDK is not installed in the current runtime") from exc

        request_shape, kwargs, tool_name_aliases = self._renderer._build_openai_request_kwargs(endpoint, request)
        if request_shape != OPENAI_RESPONSES_SHAPE:
            raise LLMEndpointInvocationError(f"endpoint {endpoint.endpoint_id} did not render a Responses request")
        client_kwargs, request_kwargs = _split_openai_responses_sdk_kwargs(kwargs)
        try:
            client = openai.OpenAI(**client_kwargs)
            response = _run_with_wall_timeout(
                lambda: client.responses.create(**request_kwargs),
                timeout_seconds=_timeout_from_openai_kwargs(kwargs),
                description=f"openai responses invocation for {endpoint.endpoint_id}",
            )
        except Exception as exc:
            raise LLMEndpointInvocationError(f"openai responses invocation failed for {endpoint.endpoint_id}: {exc}") from exc
        return self._renderer._parse_openai_responses_response(response, tool_name_aliases=tool_name_aliases)

    def invoke_stream(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> Iterable[NormalizedLLMStreamEvent]:
        yield from _stream_events_from_outcome(self.invoke(endpoint, request))


@dataclass
class AnthropicMessagesEndpointInvoker:
    credentials: LLMCredentialResolver = field(default_factory=LLMCredentialResolver)
    artifact_manager: Any = None
    last_payload_summary: dict[str, Any] = field(default_factory=dict, init=False)

    @staticmethod
    def supports_endpoint(endpoint: LLMEndpointModel) -> bool:
        provider = str(getattr(endpoint, "provider", "") or "").strip().lower()
        api_mode = str(getattr(endpoint, "api_mode", "") or "").strip().lower()
        return bool(provider == "anthropic" or api_mode == "anthropic_messages")

    def refresh_credentials(self) -> bool:
        refresh = getattr(self.credentials, "refresh", None)
        if callable(refresh):
            refresh()
            return True
        clear_cache = getattr(self.credentials, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()
            return True
        return False

    def invoke(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        try:
            import anthropic  # type: ignore
        except Exception as exc:
            raise LLMEndpointInvocationError("anthropic SDK is not installed in the current runtime") from exc

        client_kwargs, request_kwargs, tool_name_aliases = self._build_messages_kwargs(endpoint, request)
        try:
            client = anthropic.Anthropic(**client_kwargs)
            response = _run_with_wall_timeout(
                lambda: client.messages.create(**request_kwargs),
                timeout_seconds=_coerce_timeout_seconds(client_kwargs.get("timeout"), default=120.0),
                description=f"anthropic messages invocation for {endpoint.endpoint_id}",
            )
        except Exception as exc:
            raise LLMEndpointInvocationError(f"anthropic messages invocation failed for {endpoint.endpoint_id}: {exc}") from exc
        return _parse_anthropic_messages_response(response, tool_name_aliases=tool_name_aliases)

    def invoke_stream(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> Iterable[NormalizedLLMStreamEvent]:
        yield from _stream_events_from_outcome(self.invoke(endpoint, request))

    def _build_messages_kwargs(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        tool_name_aliases = _build_tool_name_aliases(request.tools)
        image_url_format = _image_url_format(endpoint)
        messages = _coerce_messages_for_openai_chat(
            list(request.messages),
            tool_name_aliases=tool_name_aliases,
            artifact_manager=self.artifact_manager,
            supports_vision=bool(endpoint.supports_vision),
            image_url_format=image_url_format,
        )
        messages = self._transform_messages(endpoint, request, messages)
        self.last_payload_summary = _summarize_provider_payload(endpoint, messages, image_url_format=image_url_format)
        system, anthropic_messages = chat_messages_to_anthropic_messages(messages)
        timeout = _coerce_timeout_seconds(request.metadata.get("timeout_seconds"), default=120.0)
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "max_retries": 0,
        }
        api_key = self.credentials.resolve_api_key(endpoint)
        if api_key:
            client_kwargs["api_key"] = api_key
        if endpoint.base_url and not str(endpoint.base_url).startswith("stub://"):
            client_kwargs["base_url"] = _openai_api_base(str(endpoint.base_url))
        request_kwargs: dict[str, Any] = {
            "model": _strip_model_provider_prefix(str(request.model_hint or endpoint.model_id or ""), {"anthropic"}),
            "messages": anthropic_messages,
            "max_tokens": request.max_output_tokens,
        }
        if system:
            request_kwargs["system"] = system
        if request.temperature is not None:
            request_kwargs["temperature"] = request.temperature
        tools = chat_tools_to_anthropic_tools(
            _coerce_tools_for_openai_chat(request.tools, tool_name_aliases=tool_name_aliases)
        )
        if tools:
            request_kwargs["tools"] = tools
        thinking = think_level_to_anthropic_thinking(
            request.metadata.get("think_level"),
            request.max_output_tokens,
        )
        if thinking is not None:
            request_kwargs["thinking"] = thinking
        return client_kwargs, request_kwargs, tool_name_aliases

    def _transform_messages(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return messages


@dataclass
class ZaiAnthropicMessagesEndpointInvoker(AnthropicMessagesEndpointInvoker):
    message_hooks: tuple[str, ...] = ()

    @staticmethod
    def supports_endpoint(endpoint: LLMEndpointModel) -> bool:
        api_mode = str(getattr(endpoint, "api_mode", "") or "").strip().lower()
        return bool(api_mode == "anthropic_messages" and is_zai_glm_endpoint(endpoint))

    def _transform_messages(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return apply_llm_message_hooks(endpoint, request, messages, hooks=self.message_hooks)


def _stream_events_from_outcome(outcome: CanonicalLLMOutcome) -> Iterable[NormalizedLLMStreamEvent]:
    provider_specific_fields = dict(getattr(outcome, "provider_specific_fields", {}) or {})
    if outcome.reasoning_text or provider_specific_fields:
        yield NormalizedLLMStreamEvent(
            event_kind=LLMStreamEventKind.REASONING_DELTA,
            reasoning_text=outcome.reasoning_text,
            provider_specific_fields=provider_specific_fields,
        )
    if outcome.text:
        yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=outcome.text)
    for tool_call in outcome.tool_calls:
        yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TOOL_CALL, tool_call=tool_call)
    if outcome.tool_calls:
        yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.TOOL_CALLS)
        return
    yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=outcome.finish_reason)


def _split_openai_responses_sdk_kwargs(kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    request_kwargs = dict(kwargs)
    api_key = request_kwargs.pop("api_key", None)
    api_base = request_kwargs.pop("api_base", None)
    timeout = request_kwargs.pop("timeout", None)
    max_retries = request_kwargs.pop("max_retries", 0)
    request_kwargs.pop("request_timeout", None)
    request_kwargs.pop("force_timeout", None)
    request_kwargs["model"] = _strip_model_provider_prefix(
        str(request_kwargs.get("model") or ""),
        {"openai", "hosted_vllm", "lm_studio", "llamafile"},
    )
    client_kwargs: dict[str, Any] = {"max_retries": int(max_retries or 0)}
    if api_key:
        client_kwargs["api_key"] = api_key
    if api_base:
        client_kwargs["base_url"] = api_base
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    return client_kwargs, request_kwargs


def _split_openai_chat_sdk_kwargs(kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    request_kwargs = dict(kwargs)
    api_key = request_kwargs.pop("api_key", None)
    api_base = request_kwargs.pop("api_base", None)
    timeout = request_kwargs.pop("timeout", None)
    max_retries = request_kwargs.pop("max_retries", 0)
    request_kwargs.pop("request_timeout", None)
    request_kwargs.pop("force_timeout", None)
    thinking = request_kwargs.pop("thinking", None)
    extra_body = dict(request_kwargs.pop("extra_body", {}) or {})
    if thinking is not None:
        extra_body["thinking"] = thinking
    if extra_body:
        request_kwargs["extra_body"] = extra_body
    client_kwargs: dict[str, Any] = {"max_retries": int(max_retries or 0)}
    if api_key:
        client_kwargs["api_key"] = api_key
    if api_base:
        client_kwargs["base_url"] = api_base
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    return client_kwargs, request_kwargs


def _strip_model_provider_prefix(model: str, providers: set[str]) -> str:
    text = str(model or "").strip()
    if "/" not in text:
        return text
    provider, name = text.split("/", 1)
    if provider.strip().lower() in providers:
        return name
    return text


def _parse_anthropic_messages_response(
    response: Any,
    *,
    tool_name_aliases: dict[str, str] | None = None,
) -> CanonicalLLMOutcome:
    payload = response.model_dump() if hasattr(response, "model_dump") else response.to_dict() if hasattr(response, "to_dict") else response
    content_blocks = list((payload or {}).get("content") or []) if isinstance(payload, dict) else []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    tool_calls: list[CanonicalToolCall] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                text_parts.append(text)
            continue
        if block_type == "thinking":
            thinking_blocks.append(dict(block))
            text = str(block.get("thinking") or block.get("text") or "")
            if text:
                reasoning_parts.append(text)
            continue
        if block_type == "redacted_thinking":
            thinking_blocks.append(dict(block))
            continue
        if block_type == "tool_use":
            name = _canonical_tool_name(str(block.get("name") or ""), tool_name_aliases)
            if name:
                tool_calls.append(
                    CanonicalToolCall(
                        name=name,
                        args=_coerce_tool_args(block.get("input")),
                        call_id=str(block.get("id") or "").strip() or None,
                    )
                )
    stop_reason = str((payload or {}).get("stop_reason") or "") if isinstance(payload, dict) else ""
    finish_reason = LLMFinishReason.TOOL_CALLS if tool_calls else _anthropic_finish_reason(stop_reason)
    outcome = CanonicalLLMOutcome(
        text="".join(text_parts),
        reasoning_text="\n".join(reasoning_parts),
        provider_specific_fields=_anthropic_provider_specific_fields(thinking_blocks, reasoning_parts),
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )
    _ensure_llm_invocation_result_has_payload(outcome)
    return outcome


def _anthropic_finish_reason(stop_reason: str) -> str:
    if stop_reason in {"end_turn", "stop_sequence"}:
        return LLMFinishReason.STOP
    if stop_reason == "tool_use":
        return LLMFinishReason.TOOL_CALLS
    return stop_reason or LLMFinishReason.STOP


def _anthropic_provider_specific_fields(
    thinking_blocks: list[dict[str, Any]],
    reasoning_parts: list[str],
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if thinking_blocks:
        fields["anthropic_thinking_blocks"] = [dict(block) for block in thinking_blocks]
    reasoning_content = "\n".join(reasoning_parts)
    if reasoning_content:
        fields["reasoning_content"] = reasoning_content
    return fields


@dataclass
class CodexCliEndpointInvoker:
    """Native Codex CLI invoker that returns Pal canonical outcomes."""

    bridge: CodexCliBridge | None = None
    artifact_manager: Any = None
    max_concurrency: int = DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY
    _semaphore: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.bridge is None:
            self.bridge = CodexCliBridge()
        self._semaphore = threading.BoundedSemaphore(max(1, int(self.max_concurrency)))

    @staticmethod
    def supports_endpoint(endpoint: LLMEndpointModel) -> bool:
        capabilities = dict(getattr(endpoint, "capabilities_blob", None) or {})
        provider = str(getattr(endpoint, "provider", "") or "").strip().lower()
        adapter = str(capabilities.get("adapter") or capabilities.get("llm_adapter") or "").strip().lower()
        base_url = str(getattr(endpoint, "base_url", "") or "").strip().lower()
        return bool(
            provider in {"codex", "codex_cli"}
            or adapter in {"codex", "codex_cli", "codex_native"}
            or capabilities.get("codex_cli")
            or capabilities.get("official_codex_cli")
            or capabilities.get("codex_native")
            or base_url.startswith("codex://")
        )

    def invoke(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        model, developer_instructions, input_items, dynamic_tools, effort = _codex_cli_request_parts(
            endpoint,
            request,
            artifact_manager=self.artifact_manager,
        )
        bridge = self._require_bridge()
        with self._semaphore:
            try:
                completion = bridge.invoke_turn(
                    model=model,
                    developer_instructions=developer_instructions,
                    input_items=input_items,
                    dynamic_tools=dynamic_tools,
                    effort=effort,
                    messages=list(request.messages or []),
                )
            except CodexBridgeError as exc:
                if not _should_retry_codex_final_answer_without_tools(exc, request, dynamic_tools):
                    raise
                completion = bridge.invoke_turn(
                    model=model,
                    developer_instructions=_codex_final_answer_instructions(developer_instructions),
                    input_items=input_items,
                    dynamic_tools=[],
                    effort=effort,
                    messages=list(request.messages or []),
                )
            if _codex_completion_repeats_prior_tool_call(completion, request):
                completion = bridge.invoke_turn(
                    model=model,
                    developer_instructions=_codex_final_answer_instructions(developer_instructions),
                    input_items=input_items,
                    dynamic_tools=[],
                    effort=effort,
                    messages=list(request.messages or []),
                )
        return _codex_completion_to_outcome(completion, endpoint=endpoint)

    def invoke_stream(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> Iterable[NormalizedLLMStreamEvent]:
        outcome = self.invoke(endpoint, request)
        for tool_call in outcome.tool_calls:
            yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TOOL_CALL, tool_call=tool_call)
        if outcome.tool_calls:
            yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.TOOL_CALLS)
            return
        if outcome.text:
            yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text=outcome.text)
        yield NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=outcome.finish_reason)

    def _require_bridge(self) -> CodexCliBridge:
        if self.bridge is None:
            self.bridge = CodexCliBridge()
        return self.bridge


@dataclass
class RoutingLLMEndpointInvoker:
    """Route native endpoint protocols to their concrete SDK invokers."""

    openai_chat_invoker: OpenAIChatEndpointInvoker = field(default_factory=OpenAIChatEndpointInvoker)
    codex_invoker: CodexCliEndpointInvoker = field(default_factory=CodexCliEndpointInvoker)
    openai_invoker: OpenAIResponsesEndpointInvoker = field(default_factory=OpenAIResponsesEndpointInvoker)
    zai_anthropic_invoker: ZaiAnthropicMessagesEndpointInvoker = field(default_factory=ZaiAnthropicMessagesEndpointInvoker)
    anthropic_invoker: AnthropicMessagesEndpointInvoker = field(default_factory=AnthropicMessagesEndpointInvoker)
    last_payload_summary: dict[str, Any] = field(default_factory=dict, init=False)

    @property
    def provider_registry(self) -> LLMProviderRegistry:
        return self.openai_chat_invoker.provider_registry

    def refresh_credentials(self) -> bool:
        refreshed = bool(self.openai_chat_invoker.refresh_credentials())
        refreshed = bool(self.openai_invoker.refresh_credentials()) or refreshed
        refreshed = bool(self.zai_anthropic_invoker.refresh_credentials()) or refreshed
        refreshed = bool(self.anthropic_invoker.refresh_credentials()) or refreshed
        return refreshed

    def refresh_provider_registry(self) -> bool:
        refreshed = bool(self.openai_chat_invoker.refresh_provider_registry())
        refreshed = bool(self.openai_invoker.refresh_provider_registry()) or refreshed
        return refreshed

    def invoke(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        selected = self._select(endpoint)
        self.last_payload_summary = {}
        try:
            return selected.invoke(endpoint, request)
        finally:
            summary = getattr(selected, "last_payload_summary", None)
            self.last_payload_summary = dict(summary or {})

    def invoke_stream(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> Iterable[NormalizedLLMStreamEvent]:
        selected = self._select(endpoint)
        self.last_payload_summary = {}
        try:
            yield from selected.invoke_stream(endpoint, request)
        finally:
            summary = getattr(selected, "last_payload_summary", None)
            self.last_payload_summary = dict(summary or {})

    def _select(self, endpoint: LLMEndpointModel) -> LLMEndpointInvokerPort:
        if self.codex_invoker.supports_endpoint(endpoint):
            return self.codex_invoker
        if self.openai_invoker.supports_endpoint(endpoint):
            return self.openai_invoker
        if self.zai_anthropic_invoker.supports_endpoint(endpoint):
            return self.zai_anthropic_invoker
        if self.anthropic_invoker.supports_endpoint(endpoint):
            return self.anthropic_invoker
        return self.openai_chat_invoker


def build_default_endpoint_invoker(
    *,
    credentials: LLMCredentialResolver | None = None,
    artifact_manager: Any = None,
    runtime_root: str | Path | None = None,
    message_hooks: tuple[str, ...] = (),
) -> RoutingLLMEndpointInvoker:
    resolver = credentials or LLMCredentialResolver()
    provider_registry = build_runtime_provider_registry()
    return RoutingLLMEndpointInvoker(
        openai_chat_invoker=OpenAIChatEndpointInvoker(
            credentials=resolver,
            artifact_manager=artifact_manager,
            runtime_root=runtime_root,
            provider_registry=provider_registry,
            message_hooks=message_hooks,
        ),
        openai_invoker=OpenAIResponsesEndpointInvoker(
            credentials=resolver,
            artifact_manager=artifact_manager,
            runtime_root=runtime_root,
            provider_registry=provider_registry,
            message_hooks=message_hooks,
        ),
        zai_anthropic_invoker=ZaiAnthropicMessagesEndpointInvoker(
            credentials=resolver,
            artifact_manager=artifact_manager,
            message_hooks=message_hooks,
        ),
        anthropic_invoker=AnthropicMessagesEndpointInvoker(
            credentials=resolver,
            artifact_manager=artifact_manager,
        ),
        codex_invoker=CodexCliEndpointInvoker(artifact_manager=artifact_manager),
    )


def _codex_cli_request_parts(
    endpoint: LLMEndpointModel,
    request: CanonicalLLMRequest,
    *,
    artifact_manager: Any = None,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]], str | None]:
    model = _strip_openai_prefix(str(request.model_hint or endpoint.model_id or ""))
    developer_instructions, input_items = _messages_to_codex_input(list(request.messages or []), artifact_manager=artifact_manager)
    dynamic_tools = _openai_tools_to_dynamic_tools(request.tools)
    effort = _think_level_to_completion_reasoning_effort(request.metadata.get("think_level"))
    return model, developer_instructions, input_items, dynamic_tools, effort


def _should_retry_codex_final_answer_without_tools(
    exc: CodexBridgeError,
    request: CanonicalLLMRequest,
    dynamic_tools: list[dict[str, Any]],
) -> bool:
    if not dynamic_tools:
        return False
    if "timed out" not in str(exc).lower():
        return False
    return any(str(message.get("role") or "").strip() == "tool" for message in list(request.messages or []))


def _codex_final_answer_instructions(developer_instructions: str) -> str:
    return "\n\n".join(
        part
        for part in (
            str(developer_instructions or "").strip(),
            (
                "Pal has already executed the available tools and included their results in the conversation. "
                "Produce the final user-facing answer now. Do not request another tool."
            ),
        )
        if part
    )


def _codex_completion_repeats_prior_tool_call(
    completion: CodexCompletion,
    request: CanonicalLLMRequest,
) -> bool:
    if completion.tool_call is None:
        return False
    if not any(str(message.get("role") or "").strip() == "tool" for message in list(request.messages or [])):
        return False
    current_name = str(completion.tool_call.name or "").strip()
    current_args = _coerce_tool_args(completion.tool_call.arguments)
    if not current_name:
        return False
    for message in list(request.messages or []):
        if str(message.get("role") or "").strip() != "assistant":
            continue
        for prior_call in list(message.get("tool_calls") or []):
            function = (prior_call or {}).get("function") or {}
            prior_name = str(function.get("name") or "").strip()
            if prior_name != current_name:
                continue
            if _coerce_tool_args(function.get("arguments")) == current_args:
                return True
    return False


def _codex_completion_to_outcome(completion: CodexCompletion, *, endpoint: LLMEndpointModel) -> CanonicalLLMOutcome:
    if completion.tool_call is not None:
        return CanonicalLLMOutcome(
            text="",
            reasoning_text="",
            tool_calls=[
                CanonicalToolCall(
                    name=str(completion.tool_call.name or "").strip(),
                    args=_coerce_tool_args(completion.tool_call.arguments),
                    call_id=str(completion.tool_call.call_id or "").strip() or None,
                )
            ],
            finish_reason=LLMFinishReason.TOOL_CALLS,
            preferred_endpoint_id=endpoint.endpoint_id,
            preferred_model_id=endpoint.model_id,
        )
    return CanonicalLLMOutcome(
        text=str(completion.text or "").strip(),
        reasoning_text="",
        tool_calls=[],
        finish_reason=LLMFinishReason.STOP,
        preferred_endpoint_id=endpoint.endpoint_id,
        preferred_model_id=endpoint.model_id,
    )


def _coerce_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except Exception:
            loaded = {}
        return dict(loaded or {}) if isinstance(loaded, dict) else {}
    return {}


@dataclass
class LLMRuntime(LLMRuntimePort):
    endpoint_resolver: EndpointResolver
    settings_repository: RuntimeSettingRepository
    endpoint_invoker: LLMEndpointInvokerPort | None = None
    config: Any = None
    safety_margin_tokens: int = 16384
    endpoint_retry_attempts: int = 3
    last_request: CanonicalLLMRequest | None = None
    last_endpoint_id: str | None = None
    last_model_id: str | None = None
    think_level: str = DEFAULT_THINK_LEVEL
    active_endpoint_id: str | None = None
    event_sink: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        if self.endpoint_invoker is None:
            self.endpoint_invoker = build_default_endpoint_invoker()
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

    @property
    def _default_request_timeout_seconds(self) -> float:
        value = (
            getattr(self.config, "llm_request_timeout_seconds", _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS)
            if self.config
            else _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
        )
        return _coerce_timeout_seconds(value, default=_DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS)

    @property
    def _default_compaction_timeout_seconds(self) -> float:
        value = (
            getattr(self.config, "llm_compaction_timeout_seconds", _DEFAULT_LLM_COMPACTION_TIMEOUT_SECONDS)
            if self.config
            else _DEFAULT_LLM_COMPACTION_TIMEOUT_SECONDS
        )
        return _coerce_timeout_seconds(value, default=_DEFAULT_LLM_COMPACTION_TIMEOUT_SECONDS)

    def _timeout_seconds_for_metadata(self, metadata: dict[str, Any]) -> float:
        explicit = metadata.get("timeout_seconds")
        if explicit is not None:
            return _coerce_timeout_seconds(explicit, default=self._default_request_timeout_seconds)
        purpose = str(metadata.get("purpose") or "").strip().lower()
        if "compaction" in purpose:
            return self._default_compaction_timeout_seconds
        return self._default_request_timeout_seconds

    def _timeout_seconds_for_request(self, request: CanonicalLLMRequest) -> float:
        return self._timeout_seconds_for_metadata(dict(request.metadata))

    def _llm_audit_dir(self) -> Path | None:
        root = getattr(self.config, "runtime_root", None) if self.config is not None else None
        if root is None:
            return None
        try:
            return Path(root) / "data" / "llm" / "audit"
        except TypeError:
            return None

    def _write_llm_failure_audit(
        self,
        *,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
        endpoint_index: int,
        endpoint_count: int,
        attempt: int,
        attempt_count: int,
        error_kind: str,
        exc: Exception,
        timeout_seconds: float,
    ) -> str:
        audit_dir = self._llm_audit_dir()
        if audit_dir is None:
            return ""
        created_at = datetime.now(timezone.utc).isoformat()
        audit_id = f"llm_failure_{created_at.replace(':', '').replace('+', 'Z')}_{uuid4().hex[:12]}"
        payload = {
            "audit_id": audit_id,
            "created_at": created_at,
            "error": {
                "kind": error_kind,
                "type": type(exc).__name__,
                "message": _short_error_text(exc),
                "empty_response": _is_empty_response_error(exc),
            },
            "endpoint": _endpoint_audit_payload(endpoint),
            "attempt": {
                "attempt": attempt,
                "max_attempts": attempt_count,
                "endpoint_index": endpoint_index,
                "endpoint_count": endpoint_count,
                "timeout_seconds": timeout_seconds,
            },
            "request_summary": _summarize_request_for_audit(request),
            "canonical_request": _canonical_request_for_audit(request),
            "provider_payload_summary": _last_provider_payload_summary_for_audit(self.endpoint_invoker),
        }
        path = audit_dir / f"{audit_id}.json"
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return ""
        return str(path)

    def refresh_runtime_settings(self) -> None:
        self.think_level = self.settings_repository.get_think_level()
        configured_active = self.settings_repository.get_active_llm_endpoint_id()
        endpoint_ids = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        self.active_endpoint_id = configured_active if configured_active in endpoint_ids else None

    def _enabled_endpoints_for_preference(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
        endpoint_fallback_policy: str | None = None,
    ) -> list[LLMEndpointModel]:
        preferred = str(preferred_endpoint_id or "").strip() or None
        preferred_source = str(preferred_endpoint_source or "").strip().lower()
        fallback_disabled = _endpoint_fallback_disabled(endpoint_fallback_policy) or (
            bool(preferred) and preferred_source in _STRICT_ENDPOINT_PREFERRED_SOURCES
        )
        if fallback_disabled:
            if preferred:
                return self.endpoint_resolver.enabled(preferred_endpoint_id=preferred, include_remaining=False)
            if self.active_endpoint_id:
                return self.endpoint_resolver.enabled(preferred_endpoint_id=self.active_endpoint_id, include_remaining=False)
            return self.endpoint_resolver.enabled(include_remaining=False)
        if preferred:
            return self.endpoint_resolver.enabled(
                preferred_endpoint_id=preferred,
                fallback_endpoint_id=self.active_endpoint_id,
            )
        return self.endpoint_resolver.enabled(preferred_endpoint_id=self.active_endpoint_id)

    def _enabled_endpoints_for_metadata(self, metadata: dict[str, Any]) -> list[LLMEndpointModel]:
        return self._enabled_endpoints_for_preference(
            preferred_endpoint_id=str(metadata.get("preferred_endpoint_id") or "").strip() or None,
            preferred_endpoint_source=str(metadata.get("preferred_endpoint_source") or "").strip() or None,
            endpoint_fallback_policy=str(metadata.get("endpoint_fallback_policy") or "").strip() or None,
        )

    def refresh_llm_endpoints(self) -> dict[str, Any]:
        before = [endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints]
        configured_active = self.settings_repository.get_active_llm_endpoint_id()
        credentials_refreshed = self._refresh_endpoint_credentials()
        provider_adapters_refreshed = self._refresh_provider_adapters()
        self.endpoint_resolver.refresh()
        self.refresh_runtime_settings()
        after = [endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints]
        primary = self.endpoint_resolver.primary(preferred_endpoint_id=self.active_endpoint_id)
        provider_registry = getattr(self.endpoint_invoker, "provider_registry", None)
        provider_adapter_load_errors = list(getattr(provider_registry, "load_errors", []) or [])
        return {
            "before_count": len(before),
            "enabled_count": len(after),
            "added_endpoint_ids": sorted(set(after) - set(before)),
            "removed_endpoint_ids": sorted(set(before) - set(after)),
            "configured_active_endpoint_id": configured_active,
            "active_endpoint_id": self.active_endpoint_id,
            "active_endpoint_available": bool(configured_active and configured_active == self.active_endpoint_id),
            "primary_endpoint_id": primary.endpoint_id if primary is not None else None,
            "primary_model_id": primary.model_id if primary is not None else None,
            "enabled_endpoint_ids": after,
            "credentials_refreshed": credentials_refreshed,
            "provider_adapters_refreshed": provider_adapters_refreshed,
            "provider_adapter_load_errors": provider_adapter_load_errors,
        }

    def _refresh_endpoint_credentials(self) -> bool:
        refresh = getattr(self.endpoint_invoker, "refresh_credentials", None)
        if callable(refresh):
            return bool(refresh())
        credentials = getattr(self.endpoint_invoker, "credentials", None)
        if credentials is None:
            return False
        credential_refresh = getattr(credentials, "refresh", None)
        if callable(credential_refresh):
            credential_refresh()
            return True
        clear_cache = getattr(credentials, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()
            return True
        return False

    def _refresh_provider_adapters(self) -> bool:
        refresh = getattr(self.endpoint_invoker, "refresh_provider_registry", None)
        if callable(refresh):
            return bool(refresh())
        provider_registry = getattr(self.endpoint_invoker, "provider_registry", None)
        if provider_registry is None:
            return False
        load_entry_points = getattr(provider_registry, "load_entry_points", None)
        if callable(load_entry_points):
            load_entry_points()
            return True
        return False

    def set_active_endpoint(self, endpoint_id: str) -> str:
        normalized = str(endpoint_id or "").strip()
        self.settings_repository.set_active_llm_endpoint_id(normalized)
        self.active_endpoint_id = normalized or None
        return normalized

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        self.refresh_runtime_settings()
        enabled = self._enabled_endpoints_for_metadata(dict(request.metadata))
        primary = enabled[0] if enabled else None
        return self._build_preflight_advice(
            endpoint=primary,
            request=request,
            fallback_chain=[endpoint.model_id for endpoint in enabled[1:]],
        )

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        return await asyncio.to_thread(self.preflight, request)

    def resolve_endpoint_facts(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> dict[str, Any]:
        self.refresh_runtime_settings()
        normalized_preferred = str(preferred_endpoint_id or "").strip() or None
        enabled = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=normalized_preferred,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint = enabled[0] if enabled else None
        if endpoint is None:
            return {
                "endpoint_id": normalized_preferred or self.active_endpoint_id,
                "model_id": None,
                "context_window": None,
                "max_output_tokens": None,
                "supports_vision": False,
                "supports_streaming": False,
                "input_modalities": [],
                "capabilities": {},
            }
        return {
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "context_window": endpoint.context_window,
            "max_output_tokens": endpoint.max_output_tokens,
            "supports_vision": bool(endpoint.supports_vision),
            "supports_streaming": bool(endpoint.supports_streaming),
            "input_modalities": list(endpoint.input_modalities_blob or []),
            "capabilities": dict(endpoint.capabilities_blob or {}),
        }

    def resolve_max_output_tokens(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> int | None:
        self.refresh_runtime_settings()
        enabled = self._enabled_endpoints_for_preference(
            preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source,
        )
        endpoint = enabled[0] if enabled else None
        if endpoint is not None and endpoint.max_output_tokens is not None:
            return endpoint.max_output_tokens
        if endpoint is not None and endpoint.context_window is not None:
            return self._max_output_tokens_from_context_window(endpoint.context_window)
        return None

    def _invoke_endpoints_with_retry(
        self,
        request: CanonicalLLMRequest,
        invoke_fn,
    ) -> _EndpointInvocationResult:
        """Shared endpoint iteration with retry, backoff, and stale-connection handling."""
        requested_preferred_endpoint_id = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        enabled = list(self._enabled_endpoints_for_metadata(dict(request.metadata)))
        if not enabled:
            effective_request = self._build_effective_request(request, endpoint=None)
            self.last_request = effective_request
            self.last_endpoint_id = None
            self.last_model_id = None
            return _EndpointInvocationResult(kind="no_endpoints")

        last_error: Exception | None = None
        had_stale_connection = False
        failed_connection_domains: set[tuple[str, str]] = set()
        for endpoint_index, endpoint in enumerate(enabled):
            endpoint_domain = _endpoint_connection_failure_domain(endpoint)
            if endpoint_domain is not None and endpoint_domain in failed_connection_domains:
                self._emit_llm_progress(
                    "llm_endpoint_skipped",
                    endpoint=endpoint,
                    endpoint_index=endpoint_index,
                    endpoint_count=len(enabled),
                    reason="shared_connection_failure_domain",
                )
                continue
            if had_stale_connection:
                time.sleep(self._retry_stale_settle_ms / 1000.0)
                had_stale_connection = False
            if endpoint_index > 0:
                self._emit_llm_progress(
                    "llm_endpoint_fallback_started",
                    endpoint=endpoint,
                    endpoint_index=endpoint_index,
                    endpoint_count=len(enabled),
                )

            effective_request = self._build_effective_request(request, endpoint=endpoint)
            self.last_request = effective_request
            advice = self._build_preflight_advice(
                endpoint=endpoint,
                request=LLMPreflightRequest(
                    messages=effective_request.messages,
                    max_output_tokens=effective_request.max_output_tokens,
                    model_hint=effective_request.model_hint,
                    tools=list(effective_request.tools),
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

            attempt_count = max(1, self.endpoint_retry_attempts)
            for attempt in range(attempt_count):
                try:
                    timeout_seconds = self._timeout_seconds_for_metadata(dict(effective_request.metadata))
                    result = _run_with_wall_timeout(
                        lambda: invoke_fn(endpoint, effective_request),
                        timeout_seconds=timeout_seconds,
                        description=f"llm endpoint invocation for {endpoint.endpoint_id}",
                    )
                    _ensure_llm_invocation_result_has_payload(result)
                    self.last_request = effective_request
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    if requested_preferred_endpoint_id is None and endpoint.endpoint_id != self.active_endpoint_id:
                        self.set_active_endpoint(endpoint.endpoint_id)
                    if endpoint_index > 0:
                        self._emit_llm_progress(
                            "llm_endpoint_fallback_succeeded",
                            endpoint=endpoint,
                            endpoint_index=endpoint_index,
                            endpoint_count=len(enabled),
                        )
                    return _EndpointInvocationResult(
                        kind="success",
                        value=result,
                        endpoint=endpoint,
                        effective_request=effective_request,
                    )
                except Exception as exc:
                    last_error = exc
                    error_kind = _classify_retry_error(exc)
                    audit_path = self._write_llm_failure_audit(
                        endpoint=endpoint,
                        request=effective_request,
                        endpoint_index=endpoint_index,
                        endpoint_count=len(enabled),
                        attempt=attempt + 1,
                        attempt_count=attempt_count,
                        error_kind=error_kind,
                        exc=exc,
                        timeout_seconds=timeout_seconds,
                    )
                    attempt_failed_payload = {
                        "endpoint_index": endpoint_index,
                        "endpoint_count": len(enabled),
                        "attempt": attempt + 1,
                        "max_attempts": attempt_count,
                        "error_kind": error_kind,
                        "error_message": _short_error_text(exc),
                        "next_endpoint_id": _next_endpoint_id(enabled, endpoint_index),
                    }
                    if audit_path:
                        attempt_failed_payload["audit_path"] = audit_path
                    self._emit_llm_progress("llm_endpoint_attempt_failed", endpoint=endpoint, **attempt_failed_payload)
                    if error_kind == "stale_connection":
                        if endpoint_domain is not None:
                            failed_connection_domains.add(endpoint_domain)
                        had_stale_connection = True
                        self._emit_llm_progress(
                            "llm_endpoint_exhausted",
                            endpoint=endpoint,
                            endpoint_index=endpoint_index,
                            endpoint_count=len(enabled),
                            attempt=attempt + 1,
                            max_attempts=attempt_count,
                            reason=error_kind,
                            next_endpoint_id=_next_endpoint_id(enabled, endpoint_index),
                        )
                        break
                    if error_kind == "connection":
                        if endpoint_domain is not None:
                            failed_connection_domains.add(endpoint_domain)
                        self._emit_llm_progress(
                            "llm_endpoint_exhausted",
                            endpoint=endpoint,
                            endpoint_index=endpoint_index,
                            endpoint_count=len(enabled),
                            attempt=attempt + 1,
                            max_attempts=attempt_count,
                            reason=error_kind,
                            next_endpoint_id=_next_endpoint_id(enabled, endpoint_index),
                        )
                        break
                    if error_kind == "timeout":
                        if endpoint_domain is not None:
                            failed_connection_domains.add(endpoint_domain)
                        self._emit_llm_progress(
                            "llm_endpoint_exhausted",
                            endpoint=endpoint,
                            endpoint_index=endpoint_index,
                            endpoint_count=len(enabled),
                            attempt=attempt + 1,
                            max_attempts=attempt_count,
                            reason=error_kind,
                            next_endpoint_id=_next_endpoint_id(enabled, endpoint_index),
                        )
                        break
                    if attempt < attempt_count - 1:
                        delay = _compute_retry_delay(
                            attempt + 1,
                            error_kind=error_kind,
                            base_delay_ms=self._retry_base_delay_ms,
                            max_delay_ms=self._retry_max_delay_ms,
                            stale_settle_ms=self._retry_stale_settle_ms,
                        )
                        self._emit_llm_progress(
                            "llm_endpoint_retry_scheduled",
                            endpoint=endpoint,
                            endpoint_index=endpoint_index,
                            endpoint_count=len(enabled),
                            attempt=attempt + 1,
                            next_attempt=attempt + 2,
                            max_attempts=attempt_count,
                            error_kind=error_kind,
                            delay_seconds=round(delay, 3),
                            next_endpoint_id=_next_endpoint_id(enabled, endpoint_index),
                        )
                        time.sleep(delay)
                    else:
                        self._emit_llm_progress(
                            "llm_endpoint_exhausted",
                            endpoint=endpoint,
                            endpoint_index=endpoint_index,
                            endpoint_count=len(enabled),
                            attempt=attempt + 1,
                            max_attempts=attempt_count,
                            reason=error_kind,
                            next_endpoint_id=_next_endpoint_id(enabled, endpoint_index),
                        )

        self.last_endpoint_id = None
        self.last_model_id = None
        reason = str(last_error) if last_error is not None else "unknown endpoint invocation error"
        return _EndpointInvocationResult(kind="error", error_message=reason)

    def _emit_llm_progress(self, phase: str, *, endpoint: LLMEndpointModel, **payload: Any) -> None:
        sink = _SCOPED_LLM_EVENT_SINK.get() or self.event_sink
        if not callable(sink):
            return
        event = {
            "phase": phase,
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "provider": endpoint.provider,
            **payload,
        }
        try:
            sink(event)
        except Exception:
            return

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
        profile: CompactionProfile = CompactionProfile.PAL,
    ) -> str:
        profile = _coerce_compaction_profile(profile)
        profile_name = profile.value
        request = CanonicalLLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Summarize the recent {profile_name} context into a short continuity summary. "
                        "Preserve user preferences, commitments, active goals, factual context, constraints, and next action. "
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
                "compaction_profile": profile_name,
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
        profile: CompactionProfile = CompactionProfile.PAL,
    ) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.summarize_compaction,
                    text,
                    max_output_tokens=max_output_tokens,
                    preferred_endpoint_id=preferred_endpoint_id,
                    preferred_model_id=preferred_model_id,
                    profile=profile,
                ),
                timeout=self._default_compaction_timeout_seconds,
            )
        except TimeoutError:
            return ""

    _COMPACT_PAL_SCHEMA = "pal.compaction.pal.v1"
    _COMPACT_MINION_SCHEMA = "pal.compaction.minion.v1"
    _COMPACT_LEGACY_SCHEMA = "pal.compaction.v2"
    _COMPACT_PAL_STRUCTURED_SYSTEM = (
        "You are a memory compaction engine.\n"
        "Read the context below and produce a structured JSON object using schema pal.compaction.pal.v1.\n"
        "\n"
        "Required top-level fields:\n"
        '  "schema": "pal.compaction.pal.v1"\n'
        '  "kind": "pal"\n'
        '  "continuity": object with fields:\n'
        "    - latest_user_intent: what the user most recently wanted, beyond the literal last sentence\n"
        "    - active_thread: the current discussion or task chain\n"
        "    - explicit_constraints: user constraints such as do-not-code, do-not-poll, timing, or scope limits\n"
        "    - decisions_made: confirmed design or implementation decisions\n"
        "    - pending_questions: unresolved questions or tradeoffs\n"
        "    - recent_user_delegated_tasks: tasks the user recently asked Pal or minions to do\n"
        "    - next_best_action: what Pal should do next after compact, if the user continues\n"
        "    - important_refs: important file paths, commit ids, run ids, test results, or artifacts\n"
        "    - stale_or_discarded_context: context that should not drive future behavior\n"
        '  "summary": a JSON object with fields:\n'
        "    - summary (string, required): compact continuity summary for prompt display\n"
        "    - search_text (string, required): compact source excerpts, identifiers, and key terms for retrieval; do not dump full transcripts\n"
        '  "memory_candidates": a list of zero or more candidate items, each with:\n'
        "    - kind (string): \"fact\" or \"case\"\n"
        "    - title (string, required): short label identifying the candidate\n"
        "    - summary (string, required): concise candidate summary for future LLM consumption\n"
        "    - source_excerpt (string, required): short source excerpt or key terms justifying the candidate\n"
        "    - why_durable (string, optional): why this may be worth long-term memory\n"
        "    - confidence (string, optional): low, medium, or high\n"
        "    - canonical_key (string, optional): stable identity key for dedup\n"
        "    - scope (string, optional): \"system\" or \"task\"\n"
        "    - task_id (string, optional)\n"
        "    - payload (object, optional): for case kind, include situation/task/action/result\n"
        "Rules:\n"
        "- Output valid JSON only, no markdown fences.\n"
        "- Write a complete bounded continuity summary, usually 1500-2500 words or less. Do not trail off, end mid-sentence, or rely on output truncation.\n"
        "- If the source is long, summarize by durable importance instead of preserving raw order.\n"
        "- Prioritize durable user preferences, stable user status/context, real goals/plans/commitments, confirmed project decisions, and long-lived constraints.\n"
        "- Do not create entries from jokes, temporary emotions, momentary frustration, speculation, transient runtime state, or unconfirmed intent.\n"
        "- Do not invent information not present in the source.\n"
        "- summary is always required and should cover the recoverable context.\n"
        "- Create memory_candidates only for stable facts, preferences, status/context, goals/plans, commitments, project facts, confirmed decisions, or explicitly reusable task/project cases.\n"
        "- memory_candidates are candidates only; they are not committed long-term memory.\n"
        "- Do not create memory_candidates for repair lessons, procedures, behavior rules, routing advice, or skill workflows unless the user explicitly asked to remember/save them as memory.\n"
        "- If nothing worth extracting, return an empty memory_candidates list.\n"
        "- title, summary, and source_excerpt/search_text serve different purposes: title is a short label; summary is compressed prompt content; source_excerpt/search_text are retrieval/audit terms.\n"
        "The pal compact tracks the user and current collaboration continuity."
    )
    _COMPACT_MINION_STRUCTURED_SYSTEM = (
        "You are a minion task-state compaction engine.\n"
        "Read the context below and produce a structured JSON object using schema pal.compaction.minion.v1.\n"
        "\n"
        "Required top-level fields:\n"
        '  "schema": "pal.compaction.minion.v1"\n'
        '  "kind": "minion"\n'
        '  "continuity": object with fields:\n'
        "    - task_goal: scoped work-order goal\n"
        "    - current_milestone_hint: compact hint for the milestone being worked\n"
        "    - claimed_completed: work that appears completed from the compact source\n"
        "    - claimed_pending: work that appears pending from the compact source\n"
        "    - implementation_decisions: decisions already made by the worker\n"
        "    - verification_hints: commands/checks/results that may matter\n"
        "    - review_or_repair_hints: reviewer findings, required fixes, or repair hints\n"
        "    - next_action_hint: first likely action after reconciling with truth sources\n"
        "    - must_verify_against: include work_order, plan_artifact, current_milestone, checkpoint_ledger, runtime_ledger, git_status, current_files\n"
        '  "summary": a JSON object with fields:\n'
        "    - summary (string, required): compact task-continuity summary for prompt display\n"
        "    - search_text (string, required): compact source excerpts, identifiers, and key terms for retrieval; do not dump full transcripts\n"
        "\n"
        "Rules:\n"
        "- Output valid JSON only, no markdown fences.\n"
        "- Write a complete bounded task-continuity summary, usually 1500-2500 words or less. Do not trail off, end mid-sentence, or rely on output truncation.\n"
        "- Minion compact is continuity reference only, not source of truth.\n"
        "- Do not claim milestone, acceptance criterion, checkpoint, or review completion as true solely from compact context.\n"
        "- The worker must verify against work order, plan artifact, checkpoint/ledger, and workspace before acting.\n"
        "- Preserve claimed completed/pending state, decisions, repair findings, verification hints, and next action.\n"
        "- Reusable lessons or case memory are proposed at work-order completion/finalization, outside compact.\n"
        "- Do not invent information not present in the source.\n"
        "- summary is always required and should cover the recoverable task context.\n"
    )

    def compact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
        profile: CompactionProfile = CompactionProfile.PAL,
    ) -> dict[str, Any]:
        profile = _coerce_compaction_profile(profile)
        system_prompt = self._COMPACT_MINION_STRUCTURED_SYSTEM if profile == CompactionProfile.MINION else self._COMPACT_PAL_STRUCTURED_SYSTEM
        request = CanonicalLLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
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
                "compaction_profile": profile.value,
            },
        )
        outcome = self.generate(request)
        if outcome.finish_reason == LLMFinishReason.COMPACT_REQUIRED:
            return {}
        raw = str(outcome.text or "").strip()
        payload = _extract_compaction_json(raw)
        return self._normalize_structured_compaction_payload(payload, profile=profile)

    def _normalize_structured_compaction_payload(self, payload: dict[str, Any], *, profile: CompactionProfile) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        schema = str(payload.get("schema") or "").strip()
        if profile == CompactionProfile.MINION:
            if schema not in {self._COMPACT_MINION_SCHEMA, self._COMPACT_LEGACY_SCHEMA}:
                return {}
            normalized = dict(payload)
            normalized["schema"] = self._COMPACT_MINION_SCHEMA
            normalized["kind"] = "minion"
            normalized.pop("memory_candidates", None)
            return normalized
        if schema not in {self._COMPACT_PAL_SCHEMA, self._COMPACT_LEGACY_SCHEMA}:
            return {}
        normalized = dict(payload)
        normalized["schema"] = self._COMPACT_PAL_SCHEMA
        normalized["kind"] = "pal"
        if not isinstance(normalized.get("memory_candidates"), list):
            normalized["memory_candidates"] = []
        return normalized

    async def acompact_memory_structured(
        self,
        text: str,
        *,
        max_output_tokens: int = 384,
        preferred_endpoint_id: str | None = None,
        preferred_model_id: str | None = None,
        profile: CompactionProfile = CompactionProfile.PAL,
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.compact_memory_structured,
                    text,
                    max_output_tokens=max_output_tokens,
                    preferred_endpoint_id=preferred_endpoint_id,
                    preferred_model_id=preferred_model_id,
                    profile=profile,
                ),
                timeout=self._default_compaction_timeout_seconds,
            )
        except TimeoutError:
            return {}

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
                    "tools_schema_chars",
                    "conversation_chars",
                    "current_user_chars",
                    "estimated_input_chars",
                    "hard_keep_chars",
                )
            }
            if "tools_schema_chars" not in snapshot:
                tools_schema_chars = _estimate_tools_schema_chars(request.tools)
                breakdown["tools_schema_chars"] = tools_schema_chars
                breakdown["estimated_input_chars"] += tools_schema_chars
                breakdown["hard_keep_chars"] += tools_schema_chars
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
        tools_schema_chars = _estimate_tools_schema_chars(request.tools)
        estimated_input_chars = system_chars + tool_protocol_chars + tools_schema_chars + conversation_chars + current_user_chars
        return {
            "system_chars": system_chars,
            "tool_protocol_chars": tool_protocol_chars,
            "tools_schema_chars": tools_schema_chars,
            "conversation_chars": conversation_chars,
            "current_user_chars": current_user_chars,
            "estimated_input_chars": estimated_input_chars,
            "hard_keep_chars": system_chars + tool_protocol_chars + tools_schema_chars + current_user_chars,
        }

    @staticmethod
    def _estimate_message_chars(message: dict[str, Any]) -> int:
        total = _message_content_chars(message.get("content"))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            try:
                total += len(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True))
            except TypeError:
                total += len(str(tool_calls))
        tool_call_id = str(message.get("tool_call_id", "") or "").strip()
        if tool_call_id:
            total += len(tool_call_id)
        provider_specific_fields = message.get("provider_specific_fields")
        if provider_specific_fields:
            try:
                total += len(json.dumps(provider_specific_fields, ensure_ascii=False, sort_keys=True))
            except TypeError:
                total += len(str(provider_specific_fields))
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

    def _max_output_tokens_from_context_window(self, context_window: int) -> int:
        cap = int(getattr(self.config, "default_max_output_tokens", 25_000) or 25_000)
        floor = int(getattr(self.config, "fallback_max_output_tokens", 4096) or 4096)
        margin = self._context_margin_tokens(int(context_window))
        usable = max(512, int(context_window) - margin)
        context_fraction = max(512, int(context_window) // 4)
        return max(512, min(cap, max(floor, context_fraction), usable))

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
        if metadata.get("timeout_seconds") is None:
            metadata["timeout_seconds"] = self._timeout_seconds_for_metadata(metadata)
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
        if isinstance(content, list):
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and str(part.get("type") or "") == "text"
            ]
            text = "\n".join(part for part in parts if part).strip()
            if text:
                return text
    return ""


def _coerce_response_mode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text


def _openai_api_base(base_url: str) -> str:
    url = str(base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/chat", "/v1/messages", "/messages"):
        if url.endswith(suffix):
            return url[: -len(suffix)].rstrip("/")
    return url


def _build_tool_name_aliases(tools: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str((function or {}).get("name") or tool.get("name") or "").strip()
        if not name:
            continue
        aliases[name] = re.sub(r"[^a-zA-Z0-9_-]+", "_", llm_tool_name(name)).strip("_") or "tool"
    return aliases


def _external_tool_name(name: str, tool_name_aliases: dict[str, str] | None) -> str:
    return str((tool_name_aliases or {}).get(name, name))


def _canonical_tool_name(name: str, tool_name_aliases: dict[str, str] | None) -> str:
    reverse = {alias: canonical for canonical, alias in (tool_name_aliases or {}).items()}
    return str(reverse.get(name, name))


def _coerce_tools_for_openai_chat(
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


def _message_content_chars(content: Any) -> int:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") == "text":
                parts.append(str(item.get("text") or ""))
        return len("\n".join(part for part in parts if part))
    return len(str(content or ""))


def _estimate_tools_schema_chars(tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    try:
        return len(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return len(str(tools))


def _coerce_messages_for_openai_chat(
    messages: list[dict[str, Any]],
    *,
    tool_name_aliases: dict[str, str] | None = None,
    artifact_manager: Any = None,
    supports_vision: bool = False,
    image_url_format: str = "data_url",
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        payload = dict(message)
        content = payload.get("content")
        if isinstance(content, list):
            payload["content"] = _coerce_content_parts_for_openai_chat(
                content,
                artifact_manager=artifact_manager,
                supports_vision=supports_vision,
                image_url_format=image_url_format,
            )
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


def _coerce_content_parts_for_openai_chat(
    content: list[Any],
    *,
    artifact_manager: Any = None,
    supports_vision: bool = False,
    image_url_format: str = "data_url",
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        part_type = str(item.get("type") or "").strip()
        if part_type == "artifact_image":
            if not supports_vision:
                continue
            source_url = str(item.get("source_url") or "").strip()
            if source_url.startswith(("http://", "https://")):
                parts.append({"type": "image_url", "image_url": {"url": source_url}})
                continue
            if artifact_manager is None:
                continue
            to_data_url = getattr(artifact_manager, "to_data_url", None)
            if not callable(to_data_url):
                continue
            data_url = to_data_url(str(item.get("representation_id") or ""))
            if not data_url:
                continue
            parts.append({"type": "image_url", "image_url": {"url": _coerce_image_url_value(data_url, image_url_format)}})
            continue
        parts.append(dict(item))
    return parts


def _image_url_format(endpoint: LLMEndpointModel) -> str:
    capabilities = dict(endpoint.capabilities_blob or {})
    configured = str(
        capabilities.get("image_url_format")
        or capabilities.get("vision_image_url_format")
        or ""
    ).strip().lower()
    if configured in {"data_url", "raw_base64"}:
        return configured
    # OpenAI-compatible VLM endpoints agree on the multipart shape, but differ
    # in accepted image_url transports. Default to the standards-friendly data
    # URL and let endpoint metadata opt into provider-specific variants.
    return "data_url"


def _coerce_image_url_value(data_url: str, image_url_format: str) -> str:
    if str(image_url_format or "").strip().lower() != "raw_base64":
        return data_url
    if "," not in data_url:
        return data_url
    return data_url.split(",", 1)[1]


def _summarize_provider_payload(
    endpoint: LLMEndpointModel,
    messages: list[dict[str, Any]],
    *,
    image_url_format: str,
) -> dict[str, Any]:
    image_parts: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            url = str((part.get("image_url") or {}).get("url") or "")
            if url.startswith("data:"):
                transport = "data_url"
                prefix = url.split(",", 1)[0][:96]
            elif url.startswith("http://") or url.startswith("https://"):
                transport = "http_url"
                prefix = url[:96]
            elif url:
                transport = "raw_base64_or_provider_specific"
                prefix = "<omitted>"
            else:
                transport = "empty"
                prefix = ""
            image_parts.append(
                {
                    "message_index": message_index,
                    "part_index": part_index,
                    "transport": transport,
                    "prefix": prefix,
                    "url_length": len(url),
                    "bytes": "omitted",
                }
            )
    return {
        "endpoint_id": endpoint.endpoint_id,
        "model_id": endpoint.model_id,
        "provider": endpoint.provider,
        "supports_vision": bool(endpoint.supports_vision),
        "image_url_format": image_url_format,
        "message_count": len(messages),
        "image_parts": image_parts,
    }


def _endpoint_audit_payload(endpoint: LLMEndpointModel) -> dict[str, Any]:
    return {
        "endpoint_id": endpoint.endpoint_id,
        "model_id": endpoint.model_id,
        "provider": endpoint.provider,
        "api_mode": getattr(endpoint, "api_mode", ""),
        "base_url": _redact_for_audit(getattr(endpoint, "base_url", "")),
        "supports_streaming": bool(getattr(endpoint, "supports_streaming", False)),
        "supports_vision": bool(getattr(endpoint, "supports_vision", False)),
    }


def _canonical_request_for_audit(request: CanonicalLLMRequest) -> dict[str, Any]:
    return {
        "messages": _redact_for_audit(list(request.messages or [])),
        "max_output_tokens": request.max_output_tokens,
        "model_hint": request.model_hint,
        "temperature": request.temperature,
        "tools": _redact_for_audit(list(request.tools or [])),
        "metadata": _redact_for_audit(dict(request.metadata or {})),
    }


def _summarize_request_for_audit(request: CanonicalLLMRequest) -> dict[str, Any]:
    messages = list(request.messages or [])
    roles = [str(message.get("role") or "") for message in messages if isinstance(message, dict)]
    adjacent_same_roles: list[dict[str, Any]] = []
    for index in range(1, len(roles)):
        if roles[index] == roles[index - 1]:
            adjacent_same_roles.append({"index": index, "role": roles[index]})
    role_counts: dict[str, int] = {}
    message_summaries: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            message_summaries.append({"index": index, "type": type(message).__name__})
            continue
        role = str(message.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
        tool_calls = list(message.get("tool_calls") or []) if isinstance(message.get("tool_calls"), list) else []
        message_summaries.append(
            {
                "index": index,
                "role": role,
                "content": _summarize_content_for_audit(message.get("content")),
                "tool_call_count": len(tool_calls),
                "tool_call_ids": [
                    str(tool_call.get("id") or "")
                    for tool_call in tool_calls
                    if isinstance(tool_call, dict) and str(tool_call.get("id") or "").strip()
                ],
                "tool_call_id": str(message.get("tool_call_id") or ""),
            }
        )
    return {
        "message_count": len(messages),
        "roles": roles,
        "role_counts": role_counts,
        "adjacent_same_roles": adjacent_same_roles,
        "tool_count": len(request.tools or []),
        "tool_names": [_tool_name_for_audit(tool) for tool in list(request.tools or [])],
        "message_summaries": message_summaries,
        "metadata": _redact_for_audit(dict(request.metadata or {})),
        "max_output_tokens": request.max_output_tokens,
        "model_hint": request.model_hint,
    }


def _summarize_content_for_audit(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"type": "text", "chars": len(content), "blank": not bool(content.strip())}
    if isinstance(content, list):
        part_types: list[str] = []
        text_chars = 0
        for part in content:
            if isinstance(part, dict):
                part_type = str(part.get("type") or "")
                part_types.append(part_type)
                if part_type == "text":
                    text_chars += len(str(part.get("text") or ""))
            else:
                part_types.append(type(part).__name__)
        return {"type": "parts", "part_count": len(content), "part_types": part_types, "text_chars": text_chars}
    if content is None:
        return {"type": "none", "chars": 0}
    return {"type": type(content).__name__, "chars": len(str(content))}


def _tool_name_for_audit(tool: Any) -> str:
    if not isinstance(tool, dict):
        return type(tool).__name__
    function = tool.get("function")
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
        if name:
            return name
    return str(tool.get("name") or tool.get("type") or "").strip()


def _last_provider_payload_summary_for_audit(invoker: Any) -> Any:
    summary = getattr(invoker, "last_payload_summary", None)
    if summary:
        return _redact_for_audit(summary)
    return {}


def _is_empty_response_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "no assistant content or tool calls" in message or "without assistant content or tool calls" in message


def _redact_for_audit(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 16:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _audit_key_is_sensitive(key_text):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _redact_for_audit(item, _depth=_depth + 1)
        return result
    if isinstance(value, list):
        return [_redact_for_audit(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_redact_for_audit(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_audit_text(value)
    return value


def _audit_key_is_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _AUDIT_REDACTED_KEYS or any(marker in normalized for marker in ("apikey", "credential", "password", "secret", "token"))


def _redact_audit_text(text: str) -> str:
    redacted = re.sub(r"(?i)(bearer\s+)[a-z0-9._\-+/=]{8,}", r"\1<redacted>", text)
    redacted = re.sub(r"(?i)(api[-_ ]?key[\"'=:\s]+)[^,\s\"']{8,}", r"\1<redacted>", redacted)
    if len(redacted) <= _AUDIT_MAX_STRING_CHARS:
        return redacted
    return f"{redacted[:_AUDIT_MAX_STRING_CHARS]}<truncated {len(redacted) - _AUDIT_MAX_STRING_CHARS} chars>"


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


def _message_provider_specific_fields(message: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    provider_fields = message.get("provider_specific_fields")
    if isinstance(provider_fields, dict):
        fields.update({str(key): value for key, value in provider_fields.items() if str(key).strip()})
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        fields["reasoning_content"] = reasoning
    return fields


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


def _parse_openai_chat_stream_chunk(
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
            events.append(
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.REASONING_DELTA,
                    reasoning_text=reasoning_content,
                    provider_specific_fields={"reasoning_content": reasoning_content},
                )
            )
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


def _coerce_compaction_profile(value: object) -> CompactionProfile:
    if isinstance(value, CompactionProfile):
        return value
    raw = str(value or "").strip().lower()
    return CompactionProfile.MINION if raw == CompactionProfile.MINION.value else CompactionProfile.PAL
