from __future__ import annotations

import asyncio
import json
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from pal.llm.adapters import LLMProviderRegistry, build_runtime_provider_registry, _think_level_to_completion_reasoning_effort
from pal.llm.codex_openai_bridge import (
    DEFAULT_CODEX_BRIDGE_MAX_CONCURRENCY,
    CodexCliBridge,
    CodexCompletion,
    CodexBridgeError,
    _messages_to_codex_input,
    _openai_tools_to_dynamic_tools,
    _strip_openai_prefix,
)
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
_DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 180.0
_DEFAULT_LLM_COMPACTION_TIMEOUT_SECONDS = 180.0


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


def _timeout_from_litellm_kwargs(kwargs: dict[str, Any]) -> float:
    for key in ("force_timeout", "request_timeout", "timeout"):
        value = kwargs.get(key)
        if value is None:
            continue
        return _coerce_timeout_seconds(value, default=120.0)
    return 120.0


def _run_litellm_with_wall_timeout(
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

    thread = threading.Thread(target=target, name="pal-litellm-call", daemon=True)
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


@dataclass
class LiteLLMEndpointInvoker:
    """Unified invoker using LiteLLM, with stub:// endpoints kept local."""

    credentials: LiteLLMCredentialResolver = field(default_factory=LiteLLMCredentialResolver)
    artifact_manager: Any = None
    runtime_root: str | Path | None = None
    provider_registry: LLMProviderRegistry = field(default_factory=build_runtime_provider_registry)
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
        return self._invoke_litellm(endpoint, request)

    def invoke_stream(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> Iterable[NormalizedLLMStreamEvent]:
        if endpoint.provider == "stub" or str(endpoint.base_url).startswith("stub://"):
            self.last_payload_summary = _summarize_provider_payload(endpoint, request.messages, image_url_format="stub")
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
            response = _run_litellm_with_wall_timeout(
                lambda: litellm.completion(**kwargs),
                timeout_seconds=_timeout_from_litellm_kwargs(kwargs),
                description=f"litellm invocation for {endpoint.endpoint_id}",
            )
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
            return _run_litellm_with_wall_timeout(
                lambda: list(
                    self._iter_litellm_stream(
                        litellm.completion(stream=True, **kwargs),
                        tool_name_aliases=tool_name_aliases,
                    )
                ),
                timeout_seconds=_timeout_from_litellm_kwargs(kwargs),
                description=f"litellm streaming invocation for {endpoint.endpoint_id}",
            )
        except Exception as exc:
            raise LLMEndpointInvocationError(f"litellm invocation failed for {endpoint.endpoint_id}: {exc}") from exc

    def _build_completion_kwargs(
        self,
        endpoint: LLMEndpointModel,
        request: CanonicalLLMRequest,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        tool_name_aliases = _build_tool_name_aliases(request.tools)
        image_url_format = _image_url_format(endpoint)
        messages = _coerce_messages_for_litellm(
            list(request.messages),
            tool_name_aliases=tool_name_aliases,
            artifact_manager=self.artifact_manager,
            supports_vision=bool(endpoint.supports_vision),
            image_url_format=image_url_format,
        )
        adapter = self.provider_registry.resolve(endpoint)
        self.last_payload_summary = _summarize_provider_payload(endpoint, messages, image_url_format=image_url_format)
        draft = adapter.new_draft(messages)
        timeout_seconds = request.metadata.get("timeout_seconds")
        if timeout_seconds is not None:
            try:
                timeout_value = max(1, int(float(timeout_seconds)))
                draft.timeout = timeout_value
                draft.request_timeout = float(timeout_value)
                draft.force_timeout = float(timeout_value)
            except (TypeError, ValueError):
                pass
        if endpoint.base_url and not str(endpoint.base_url).startswith("stub://"):
            draft.api_base = _litellm_api_base(str(endpoint.base_url))
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
            draft.max_tokens = request.max_output_tokens
        tools = _coerce_tools_for_litellm(request.tools, tool_name_aliases=tool_name_aliases)
        if tools:
            draft.tools = tools
            draft.tool_choice = "auto"
        adapter.apply_request(request, draft)
        return draft.to_kwargs(), tool_name_aliases

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
    """Route native providers before falling back to LiteLLM."""

    litellm_invoker: LiteLLMEndpointInvoker = field(default_factory=LiteLLMEndpointInvoker)
    codex_invoker: CodexCliEndpointInvoker = field(default_factory=CodexCliEndpointInvoker)

    @property
    def provider_registry(self) -> LLMProviderRegistry:
        return self.litellm_invoker.provider_registry

    def refresh_credentials(self) -> bool:
        return bool(self.litellm_invoker.refresh_credentials())

    def refresh_provider_registry(self) -> bool:
        return bool(self.litellm_invoker.refresh_provider_registry())

    def invoke(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        return self._select(endpoint).invoke(endpoint, request)

    def invoke_stream(self, endpoint: LLMEndpointModel, request: CanonicalLLMRequest) -> Iterable[NormalizedLLMStreamEvent]:
        yield from self._select(endpoint).invoke_stream(endpoint, request)

    def _select(self, endpoint: LLMEndpointModel) -> LLMEndpointInvokerPort:
        if self.codex_invoker.supports_endpoint(endpoint):
            return self.codex_invoker
        return self.litellm_invoker


def build_default_endpoint_invoker(
    *,
    credentials: LiteLLMCredentialResolver | None = None,
    artifact_manager: Any = None,
    runtime_root: str | Path | None = None,
) -> RoutingLLMEndpointInvoker:
    return RoutingLLMEndpointInvoker(
        litellm_invoker=LiteLLMEndpointInvoker(
            credentials=credentials or LiteLLMCredentialResolver(),
            artifact_manager=artifact_manager,
            runtime_root=runtime_root,
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

    def refresh_runtime_settings(self) -> None:
        self.think_level = self.settings_repository.get_think_level()
        configured_active = self.settings_repository.get_active_llm_endpoint_id()
        endpoint_ids = {endpoint.endpoint_id for endpoint in self.endpoint_resolver.endpoints}
        self.active_endpoint_id = configured_active if configured_active in endpoint_ids else None

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
                "supports_vision": False,
                "input_modalities": [],
                "capabilities": {},
            }
        return {
            "endpoint_id": endpoint.endpoint_id,
            "model_id": endpoint.model_id,
            "context_window": endpoint.context_window,
            "max_output_tokens": endpoint.max_output_tokens,
            "supports_vision": bool(endpoint.supports_vision),
            "input_modalities": list(endpoint.input_modalities_blob or []),
            "capabilities": dict(endpoint.capabilities_blob or {}),
        }

    def resolve_max_output_tokens(self, *, preferred_endpoint_id: str | None = None) -> int | None:
        self.refresh_runtime_settings()
        endpoint = self.endpoint_resolver.primary(preferred_endpoint_id=preferred_endpoint_id or self.active_endpoint_id)
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
                    result = invoke_fn(endpoint, effective_request)
                    self.last_request = effective_request
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    if explicit_preferred_endpoint_id is None and endpoint.endpoint_id != self.active_endpoint_id:
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
                    self._emit_llm_progress(
                        "llm_endpoint_attempt_failed",
                        endpoint=endpoint,
                        endpoint_index=endpoint_index,
                        endpoint_count=len(enabled),
                        attempt=attempt + 1,
                        max_attempts=attempt_count,
                        error_kind=error_kind,
                        error_message=_short_error_text(exc),
                        next_endpoint_id=_next_endpoint_id(enabled, endpoint_index),
                    )
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
        sink = self.event_sink
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
        timeout_seconds = self._timeout_seconds_for_request(request)
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.generate, request), timeout=timeout_seconds)
        except TimeoutError:
            return CanonicalLLMOutcome(
                text=f"LLM generation timed out after {timeout_seconds:.0f}s.",
                reasoning_text="",
                tool_calls=[],
                finish_reason=LLMFinishReason.ERROR,
            )

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
        timeout_seconds = self._timeout_seconds_for_request(request)
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.generate_stream, request), timeout=timeout_seconds)
        except TimeoutError:
            msg = f"LLM generation timed out after {timeout_seconds:.0f}s."
            return [
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.ERROR,
                    error_text=msg,
                    finish_reason=LLMFinishReason.ERROR,
                ),
                NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.ERROR),
            ]

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
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.summarize_compaction,
                    text,
                    max_output_tokens=max_output_tokens,
                    preferred_endpoint_id=preferred_endpoint_id,
                    preferred_model_id=preferred_model_id,
                ),
                timeout=self._default_compaction_timeout_seconds,
            )
        except TimeoutError:
            return ""

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
        "- Write a complete bounded rolling summary, usually 1500-2500 words or less. Do not trail off, end mid-sentence, or rely on output truncation.\n"
        "- If the source is long, summarize by durable importance instead of preserving raw order.\n"
        "- Prioritize durable user preferences, stable user status/context, real goals/plans/commitments, confirmed project decisions, and long-lived constraints.\n"
        "- Create fact entries only for stable facts, preferences, status/context, goals/plans, commitments, project facts, or confirmed decisions that should survive compaction.\n"
        "- Create case entries only for explicitly durable task/project episodes with situation/task/action/result that should be reusable as memory.\n"
        "- Do not create entries from jokes, temporary emotions, momentary frustration, speculation, transient runtime state, or unconfirmed intent.\n"
        "- Do not create entries for repair lessons, procedures, behavior rules, routing advice, or skill workflows unless the user explicitly asked to remember/save them as memory. Keep them in the rolling summary if they matter for continuity.\n"
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
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.compact_memory_structured,
                    text,
                    max_output_tokens=max_output_tokens,
                    preferred_endpoint_id=preferred_endpoint_id,
                    preferred_model_id=preferred_model_id,
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
    return ""


def _coerce_response_mode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text


def _litellm_api_base(base_url: str) -> str:
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


def _coerce_messages_for_litellm(
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
            payload["content"] = _coerce_content_parts_for_litellm(
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


def _coerce_content_parts_for_litellm(
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
