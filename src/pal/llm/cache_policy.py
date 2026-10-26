"""Trusted capability and mode selection, independent of mutable cache history."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping
from pal.llm.cache_capabilities import CacheProfileError, PromptCacheDialect
from pal.llm.shapes.base import ShapeContext

@dataclass(frozen=True)
class CacheProfile:
    """Narrow, immutable cache strategy selection for one provider family.

    Selection only: a profile chooses a dialect and wire flags. It owns no
    cache state, no secrets, and no planner internals. Unconfigured endpoints
    use implicit caching. Legacy names select the new strategies.
    """

    profile_id: str
    strategy: str
    dialect: PromptCacheDialect
    allow_explicit_breakpoints: bool = True
    allow_stable_anchor_marker: bool = False
    stable_prompt_cache_key: bool = True
    stable_session_id: bool = True
    telemetry_usage_required: bool = True
    version: str = "3"


# Legacy configuration aliases. Protocol binding comes from the endpoint,
# independently of the historical provider-specific profile name.
CACHE_PROFILES: Mapping[str, CacheProfile] = MappingProxyType({
    "openai_explicit_economic_v1": CacheProfile("openai_explicit_economic_v1", "eager_tail",
        PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT, version="3"),
    "openrouter_astra_legacy_explicit": CacheProfile(
        profile_id="openrouter_astra_legacy_explicit",
        strategy="eager_tail",
        dialect=PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT,
    ),
    "openrouter_astra_provider_implicit": CacheProfile(
        profile_id="openrouter_astra_provider_implicit",
        strategy="provider_implicit",
        dialect=PromptCacheDialect.OPENROUTER_AUTOMATIC,
        allow_explicit_breakpoints=False,
    ),
    "openrouter_astra_hybrid_anchor": CacheProfile(
        profile_id="openrouter_astra_hybrid_anchor",
        strategy="fixed_anchors",
        dialect=PromptCacheDialect.OPENROUTER_AUTOMATIC,
        allow_explicit_breakpoints=False,
        allow_stable_anchor_marker=True,
    ),
})


def cache_options(context: ShapeContext) -> Mapping[str, Any]:
    options = context.capabilities.get("prompt_cache", {}) or {}
    if not isinstance(options, Mapping):
        raise CacheProfileError("prompt_cache must be an object")
    return options


def native_dialect(context: ShapeContext) -> PromptCacheDialect:
    return context.cache_capabilities.explicit_dialect


def available_modes(context: ShapeContext) -> tuple[str, ...]:
    dialect = native_dialect(context)
    if dialect in OPENAI_EXPLICIT:
        return ("implicit", "hybrid", "explicit")
    if dialect in ANTHROPIC_EXPLICIT:
        return ("implicit", "explicit")
    return ("implicit",)


OPENAI_EXPLICIT = frozenset((PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT,
    PromptCacheDialect.OPENAI_CHAT_EXPLICIT, PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT))
ANTHROPIC_EXPLICIT = frozenset((PromptCacheDialect.ANTHROPIC_EXPLICIT,
    PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT))


def resolve_profile(context: ShapeContext) -> CacheProfile | None:
    options = cache_options(context)
    if options.get("enabled") is False or "mode" in options:
        return None
    name = str(options.get("cache_profile") or "").strip()
    if not name:
        return None
    if name not in CACHE_PROFILES:
        raise CacheProfileError(f"unknown cache_profile: {name}")
    profile = CACHE_PROFILES[name]
    if native_dialect(context) not in OPENAI_EXPLICIT:
        raise CacheProfileError(f"cache_profile {name} requires a supported OpenAI cache endpoint")
    requested = options.get("dialect")
    if requested and requested not in (profile.dialect.value, native_dialect(context).value):
        raise CacheProfileError("cache_profile conflicts with explicit dialect")
    return profile


def resolve_mode(context: ShapeContext) -> str:
    options = cache_options(context)
    if options.get("enabled") is False:
        return "disabled"
    if "mode" in options:
        mode = str(options["mode"])
    else:
        profile = resolve_profile(context)
        if profile:
            mode = ("hybrid" if profile.allow_stable_anchor_marker else
                    "explicit" if profile.allow_explicit_breakpoints else "implicit")
        elif options.get("dialect"):
            try:
                dialect = PromptCacheDialect(options["dialect"])
            except ValueError as exc:
                raise CacheProfileError(f"unknown cache dialect: {options['dialect']}") from exc
            if dialect == PromptCacheDialect.NONE:
                return "disabled"
            mode = "explicit" if dialect in OPENAI_EXPLICIT | ANTHROPIC_EXPLICIT else "implicit"
            if mode == "explicit" and dialect != native_dialect(context):
                raise CacheProfileError("cache dialect does not match endpoint protocol")
        else:
            mode = "implicit"
    if mode not in available_modes(context):
        raise CacheProfileError(f"cache mode {mode!r} unavailable; supported: {', '.join(available_modes(context))}")
    return mode


def resolve_dialect(context: ShapeContext) -> PromptCacheDialect:
    mode = resolve_mode(context)
    if mode == "disabled":
        return PromptCacheDialect.NONE
    if mode in ("explicit", "hybrid"):
        return native_dialect(context)
    # Implicit means no local caching controls, including Anthropic auto opt-in.
    return context.cache_capabilities.implicit_dialect


def validate_cache_policy(context: ShapeContext) -> None:
    resolve_dialect(context)
