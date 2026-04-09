from __future__ import annotations

import asyncio
import re
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
    safety_margin_tokens: int = 256
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

    def generate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        self.refresh_runtime_settings()
        explicit_preferred_endpoint_id = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        preferred_endpoint_id = explicit_preferred_endpoint_id or self.active_endpoint_id
        enabled = list(self.endpoint_resolver.enabled(preferred_endpoint_id=preferred_endpoint_id))
        if not enabled:
            self.last_request = self._build_effective_request(request, endpoint=None)
            self.last_endpoint_id = None
            self.last_model_id = None
            return CanonicalLLMOutcome(
                text="LLM generation failed: no enabled endpoints are configured.",
                reasoning_text="",
                tool_calls=[],
                finish_reason=LLMFinishReason.ERROR,
            )

        last_error: Exception | None = None
        for endpoint in enabled:
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
                return CanonicalLLMOutcome(
                    text="",
                    reasoning_text="",
                    tool_calls=[],
                    finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                    target_input_budget=advice.target_input_budget,
                    reserved_output_tokens=advice.reserved_output_tokens,
                    preferred_endpoint_id=endpoint.endpoint_id,
                    preferred_model_id=endpoint.model_id,
                )
            for _attempt in range(max(1, self.endpoint_retry_attempts)):
                try:
                    outcome = self._require_invoker().invoke(endpoint, effective_request)
                    self.last_request = effective_request
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    if explicit_preferred_endpoint_id is None and endpoint.endpoint_id != self.active_endpoint_id:
                        self.set_active_endpoint(endpoint.endpoint_id)
                    return outcome
                except Exception as exc:
                    last_error = exc

        self.last_endpoint_id = None
        self.last_model_id = None
        reason = str(last_error) if last_error is not None else "unknown endpoint invocation error"
        return CanonicalLLMOutcome(
            text=f"LLM generation failed after exhausting all configured endpoints: {reason}",
            reasoning_text="",
            tool_calls=[],
            finish_reason=LLMFinishReason.ERROR,
        )

    async def agenerate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        return await asyncio.to_thread(self.generate, request)

    def generate_stream(self, request: CanonicalLLMRequest) -> list[NormalizedLLMStreamEvent]:
        self.refresh_runtime_settings()
        explicit_preferred_endpoint_id = str(request.metadata.get("preferred_endpoint_id") or "").strip() or None
        preferred_endpoint_id = explicit_preferred_endpoint_id or self.active_endpoint_id
        enabled = list(self.endpoint_resolver.enabled(preferred_endpoint_id=preferred_endpoint_id))
        if not enabled:
            self.last_request = self._build_effective_request(request, endpoint=None)
            self.last_endpoint_id = None
            self.last_model_id = None
            return [
                NormalizedLLMStreamEvent(
                    event_kind=LLMStreamEventKind.ERROR,
                    error_text="LLM generation failed: no enabled endpoints are configured.",
                    finish_reason=LLMFinishReason.ERROR,
                ),
                NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.ERROR),
            ]

        last_error: Exception | None = None
        for endpoint in enabled:
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
                return [
                    NormalizedLLMStreamEvent(
                        event_kind=LLMStreamEventKind.COMPACT_REQUIRED,
                        finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                        target_input_budget=advice.target_input_budget,
                        reserved_output_tokens=advice.reserved_output_tokens,
                        preferred_endpoint_id=endpoint.endpoint_id,
                        preferred_model_id=endpoint.model_id,
                    )
                ]
            for _attempt in range(max(1, self.endpoint_retry_attempts)):
                try:
                    events = list(self._require_invoker().invoke_stream(endpoint, effective_request))
                    self.last_request = effective_request
                    self.last_endpoint_id = endpoint.endpoint_id
                    self.last_model_id = endpoint.model_id
                    if explicit_preferred_endpoint_id is None and endpoint.endpoint_id != self.active_endpoint_id:
                        self.set_active_endpoint(endpoint.endpoint_id)
                    return events
                except Exception as exc:
                    last_error = exc

        self.last_endpoint_id = None
        self.last_model_id = None
        reason = str(last_error) if last_error is not None else "unknown endpoint invocation error"
        return [
            NormalizedLLMStreamEvent(
                event_kind=LLMStreamEventKind.ERROR,
                error_text=f"LLM generation failed after exhausting all configured endpoints: {reason}",
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

    def _build_preflight_advice(
        self,
        *,
        endpoint: LLMEndpointModel | None,
        request: LLMPreflightRequest,
        fallback_chain: list[str],
    ) -> LLMPreflightAdvice:
        estimated_size = sum(len(str(message.get("content", ""))) for message in request.messages)
        reserved_output_tokens = request.max_output_tokens
        if endpoint is not None and endpoint.max_output_tokens is not None:
            reserved_output_tokens = min(request.max_output_tokens, endpoint.max_output_tokens)
        if endpoint is None or endpoint.context_window is None:
            target_budget = max(estimated_size, reserved_output_tokens)
        else:
            target_budget = max(
                endpoint.context_window - reserved_output_tokens - self.safety_margin_tokens,
                reserved_output_tokens,
            )
        active_model = endpoint.model_id if endpoint is not None else request.model_hint
        status = LLMPreflightStatus.COMPACT_REQUIRED if estimated_size > target_budget else LLMPreflightStatus.READY
        return LLMPreflightAdvice(
            status=status,
            active_model=active_model,
            fallback_chain=list(fallback_chain),
            target_input_budget=target_budget,
            reserved_output_tokens=reserved_output_tokens,
        )

    def _build_effective_request(
        self,
        request: CanonicalLLMRequest,
        *,
        endpoint: LLMEndpointModel | None,
    ) -> CanonicalLLMRequest:
        metadata: dict[str, Any] = {
            **dict(request.metadata),
            "think_level": self.think_level,
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
