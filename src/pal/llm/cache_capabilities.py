"""Bind endpoint cache capabilities before generic cache policy runs.

Provider/model presets preserve existing configuration defaults. An explicit
endpoint protocol always wins; unrecognized endpoints use the general preset.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

from pal.llm.ir import WireShape

if TYPE_CHECKING:
    from pal.llm.shapes.base import ShapeContext


class CacheProfileError(ValueError):
    """Reject an unsupported cache policy before invoking the provider."""


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
class PromptCacheCapabilities:
    protocol: str = "general"
    explicit_dialect: PromptCacheDialect = PromptCacheDialect.NONE
    implicit_dialect: PromptCacheDialect = PromptCacheDialect.NONE


_EXPLICIT_SHAPES = {
    PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT: frozenset((WireShape.OPENAI_RESPONSE,)),
    PromptCacheDialect.OPENAI_CHAT_EXPLICIT: frozenset((WireShape.OPENAI_COMPLETION,)),
    PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT: frozenset((WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION)),
    PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT: frozenset((WireShape.ANTHROPIC_MESSAGES,)),
    PromptCacheDialect.ANTHROPIC_EXPLICIT: frozenset((WireShape.ANTHROPIC_MESSAGES,)),
}
_IMPLICIT_DIALECTS = {
    PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT: PromptCacheDialect.OPENAI_AUTOMATIC,
    PromptCacheDialect.OPENAI_CHAT_EXPLICIT: PromptCacheDialect.OPENAI_AUTOMATIC,
    PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT: PromptCacheDialect.OPENROUTER_AUTOMATIC,
    PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT: PromptCacheDialect.OPENROUTER_AUTOMATIC,
}


def bind_prompt_cache_capabilities(
    *, wire_shape: WireShape, provider_id: str, model_id: str, base_url: str,
    capabilities: Mapping[str, Any],
) -> PromptCacheCapabilities:
    options = capabilities.get("prompt_cache", {})
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise CacheProfileError("prompt_cache must be an object")
    ttl = options.get("reminder_ttl_seconds")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
        raise CacheProfileError("reminder_ttl_seconds must be a positive integer")
    configured = options.get("protocol")
    if configured is not None:
        if configured == "general":
            return PromptCacheCapabilities()
        try:
            dialect = PromptCacheDialect(configured)
        except (TypeError, ValueError) as exc:
            raise CacheProfileError(f"unknown cache protocol: {configured}") from exc
        if dialect == PromptCacheDialect.NONE:
            return PromptCacheCapabilities()
        accepted_shapes = _EXPLICIT_SHAPES.get(dialect)
        if accepted_shapes is None:
            accepted_shapes = (frozenset((WireShape.ANTHROPIC_MESSAGES,))
                               if dialect == PromptCacheDialect.OPENROUTER_ANTHROPIC_AUTOMATIC
                               else frozenset((WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION)))
        if wire_shape not in accepted_shapes:
            raise CacheProfileError(f"cache protocol {dialect.value} does not support {wire_shape.value}")
        return PromptCacheCapabilities(
            dialect.value,
            dialect if dialect in _EXPLICIT_SHAPES else PromptCacheDialect.NONE,
            _IMPLICIT_DIALECTS.get(dialect, PromptCacheDialect.NONE)
            if dialect in _EXPLICIT_SHAPES else dialect,
        )
    return _default_capabilities(wire_shape, provider_id, model_id, base_url)


def _default_capabilities(
    shape: WireShape, provider_id: str, model_id: str, base_url: str,
) -> PromptCacheCapabilities:
    """Compatibility presets used only while binding an endpoint context."""
    if shape == WireShape.GEMINI_INTERACTIONS:
        return PromptCacheCapabilities()
    provider = provider_id.strip().lower()
    gateway = provider == "openrouter" or urlsplit(base_url or "").hostname == "openrouter.ai"
    implicit = (PromptCacheDialect.OPENROUTER_AUTOMATIC if gateway else
                PromptCacheDialect.OPENAI_AUTOMATIC if provider in {"openai", "chatgpt"} else PromptCacheDialect.NONE)
    explicit = PromptCacheDialect.NONE
    if shape == WireShape.ANTHROPIC_MESSAGES and (gateway or provider == "anthropic"):
        explicit = (PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT if gateway
                    else PromptCacheDialect.ANTHROPIC_EXPLICIT)
    elif ((gateway or provider in {"openai", "chatgpt"})
          and re.fullmatch(r"(?:gpt-5\.6|gpt-6-astra)(?:-[a-z0-9.-]+)?|gpt-6-(?:sol|luna)|gpt-6\.1-(?:sol|terra)|gpt-6\.2-(?:astra|sol)",
                           model_id.lower().removeprefix("openai/"))
          and shape in (WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION)):
        explicit = (PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT if gateway else
                    PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT if shape == WireShape.OPENAI_RESPONSE
                    else PromptCacheDialect.OPENAI_CHAT_EXPLICIT)
    protocol = explicit.value if explicit != PromptCacheDialect.NONE else (
        implicit.value if implicit != PromptCacheDialect.NONE else "general")
    return PromptCacheCapabilities(protocol, explicit, implicit)


def supports_response_instructions(shape: WireShape | str, capabilities: Mapping[str, Any]) -> bool:
    """Responses compatibility promises the standard request-level field.

    This makes no promise about where the provider renders or caches it.
    """
    return shape == WireShape.OPENAI_RESPONSE and capabilities.get("request_instruction_mode", "auto") != "chronological"

def supports_root_instructions(shape: WireShape | str, capabilities: Mapping[str, Any]) -> bool:
    """Standard request-local system guidance, without a suffix-cache promise."""
    return (shape in {WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES}
            and capabilities.get("request_instruction_mode", "auto") != "chronological")


def supports_suffix_instructions(shape: WireShape | str, model_id: str, base_url: str) -> bool:
    """Documented GPT-6.2 Responses suffix, not a generic compatible API."""
    return (shape == WireShape.OPENAI_RESPONSE
            and model_id.removeprefix("openai/") in {"gpt-6.2-astra", "gpt-6.2-sol"}
            and base_url.rstrip("/") in {"https://api.openai.com/v1", "https://openrouter.ai/api/v1"})


def explicit_lookup_limit(context: ShapeContext) -> int:
    # Subscription and unverified compatible routes retain conservative capacity.
    if (context.wire_shape == WireShape.OPENAI_RESPONSE
            and context.base_url.rstrip("/") in {"https://api.openai.com/v1", "https://openrouter.ai/api/v1"}
            and not context.capabilities.get("access_profile")):
        return 52
    return 4


ANTHROPIC_CLEAR_AT_BETA = "mid-conversation-system-clear-at-2026-08-21"
_CLAUDE_SCOPED_MODELS = frozenset({
    "claude-fable-5-2", "claude-fable-5-1", "claude-fable-5",
    "claude-mythos-5-2", "claude-mythos-5-1", "claude-mythos-5",
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
    "claude-sonnet-5-5", "claude-haiku-5-5",
})


def canonical_claude_model(model_id: str) -> str:
    return re.sub(r"-\d{8}$", "", model_id.lower().removeprefix("anthropic/").replace(".", "-"))


def supports_turn_scoped_instructions(shape: WireShape | str, model_id: str, base_url: str,
                                      capabilities: Mapping[str, Any]) -> bool:
    if shape != WireShape.ANTHROPIC_MESSAGES or canonical_claude_model(model_id) not in _CLAUDE_SCOPED_MODELS:
        return False
    mode = capabilities.get("request_instruction_mode", "auto")
    if mode not in {"auto", "turn_scoped"}:
        return False
    url = base_url.rstrip("/")
    # Direct Claude has the documented feature. A compatible gateway must be
    # explicitly configured after verifying its native schema and beta support.
    return url == "https://api.anthropic.com" or (
        url == "https://openrouter.ai/api" and model_id.startswith("anthropic/") and mode == "turn_scoped")


ANTHROPIC_EFFORT_UPDATE_BETA = "mid-conversation-output-config-2026-07-01"
_OPENAI_EFFORT_MODELS = frozenset({
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra",
    "gpt-6.2-astra", "gpt-6.2-sol",
})
_CLAUDE_EFFORT_MODELS = frozenset({
    "claude-fable-5-2", "claude-fable-5-1", "claude-mythos-5-2", "claude-mythos-5-1",
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5",
})


def supports_effort_updates(shape: WireShape | str, model_id: str, base_url: str,
                            capabilities: Mapping[str, Any]) -> bool:
    """Only documented native APIs and independently verified gateway routes."""
    if capabilities.get("access_profile"):
        return False
    url = base_url.rstrip("/")
    if shape == WireShape.OPENAI_RESPONSE:
        model = model_id.removeprefix("openai/")
        return (url == "https://api.openai.com/v1" and model in _OPENAI_EFFORT_MODELS
                or url == "https://openrouter.ai/api/v1" and model_id == "openai/gpt-6.2-sol")
    if shape == WireShape.ANTHROPIC_MESSAGES:
        model = canonical_claude_model(model_id)
        return (url == "https://api.anthropic.com" and model in _CLAUDE_EFFORT_MODELS
                or url == "https://openrouter.ai/api" and model_id.startswith("anthropic/")
                and model == "claude-opus-5-5")
    return False


def supports_interaction_instructions(shape: WireShape | str, model_id: str, base_url: str,
                                      capabilities: Mapping[str, Any]) -> bool:
    # Changing the root system_instruction can invalidate the prompt cache.
    # Normal requests have no temporary instruction.
    return (shape == WireShape.GEMINI_INTERACTIONS
            and capabilities.get("request_instruction_mode", "auto") in {"auto", "interaction"})


def supports_request_instructions(shape: WireShape | str, model_id: str, base_url: str,
                                  capabilities: Mapping[str, Any]) -> bool:
    return (supports_response_instructions(shape, capabilities)
            or supports_root_instructions(shape, capabilities)
            or supports_interaction_instructions(shape, model_id, base_url, capabilities))


@dataclass(frozen=True)
class KeepaliveProfile:
    """Billing and renewal data; the codec owns the actual request shape."""
    read_multiplier: float
    write_multiplier: float
    output_multiplier: float
    ttl_seconds: int
    output_limit: int
    input_price: float  # USD per million ordinary input tokens


def keepalive_profile(spec: Any, ttls: tuple[str, ...]) -> KeepaliveProfile | None:
    if spec.auth_kind != "api_key_ref" or spec.transport != "http" or not ttls:
        return None
    url, model = spec.base_url.rstrip("/"), spec.model_id
    shape = spec.wire_shape
    if shape == "openai_response" and (url == "https://api.openai.com/v1" or
            (url == "https://openrouter.ai/api/v1" and model.startswith("openai/"))):
        prices = {"gpt-6.2-sol": (2., .05), "gpt-6.2-astra": (8., .05),
                  "gpt-6.1-sol": (2., .05), "gpt-6.1-terra": (.5, .05),
                  "gpt-6-luna": (.05, .1), "gpt-6-astra": (10., .1)}
        price = prices.get(model.removeprefix("openai/"))
        if price and set(ttls) == {"30m"}:
            return KeepaliveProfile(price[1], 1.25, 5., 1800, 16, price[0])
    if (shape == "anthropic_messages" and canonical_claude_model(model) == "claude-opus-5-5"
            and (url == "https://api.anthropic.com" or
                 (url == "https://openrouter.ai/api" and model.startswith("anthropic/")))
            and set(ttls) <= {"5m", "1h"}):
        return KeepaliveProfile(.05, 1.25 if "5m" in ttls else 2., 5.,
                                300 if "5m" in ttls else 3600, 1 if url == "https://openrouter.ai/api" else 0, 4.)
    return None
