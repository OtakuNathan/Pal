"""Bind endpoint cache capabilities before generic cache policy runs.

Provider/model presets preserve existing configuration defaults. An explicit
endpoint protocol always wins; unrecognized endpoints use the general preset.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pal.llm.ir import WireShape


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
    provider = provider_id.strip().lower()
    gateway = provider == "openrouter" or urlsplit(base_url or "").hostname == "openrouter.ai"
    implicit = (PromptCacheDialect.OPENROUTER_AUTOMATIC if gateway else
                PromptCacheDialect.OPENAI_AUTOMATIC if provider == "openai" else PromptCacheDialect.NONE)
    explicit = PromptCacheDialect.NONE
    if shape == WireShape.ANTHROPIC_MESSAGES and (gateway or provider == "anthropic"):
        explicit = (PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT if gateway
                    else PromptCacheDialect.ANTHROPIC_EXPLICIT)
    elif ((gateway or provider == "openai")
          and re.fullmatch(r"gpt-5\.6(?:-[a-z0-9.-]+)?|gpt-6-astra(?:-[a-z0-9.-]+)?",
                           model_id.lower().removeprefix("openai/"))
          and shape in (WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION)):
        explicit = (PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT if gateway else
                    PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT if shape == WireShape.OPENAI_RESPONSE
                    else PromptCacheDialect.OPENAI_CHAT_EXPLICIT)
    protocol = explicit.value if explicit != PromptCacheDialect.NONE else (
        implicit.value if implicit != PromptCacheDialect.NONE else "general")
    return PromptCacheCapabilities(protocol, explicit, implicit)
