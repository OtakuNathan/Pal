from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pal.llm.ir import LLMRequestIR, LLMResponseUpdate, WireShape


class ProviderResponseHookError(RuntimeError):
    """A provider response could not be normalized into Pal's LLM IR."""


@dataclass(frozen=True)
class ProviderResponseHookContext:
    endpoint_id: str
    provider_id: str
    model_id: str
    wire_shape: WireShape
    request: LLMRequestIR


ResponseNormalizer = Callable[
    [ProviderResponseHookContext, Iterable[LLMResponseUpdate]],
    Iterable[LLMResponseUpdate],
]


@dataclass(frozen=True)
class ProviderResponseHook:
    provider_id: str
    normalize_updates: ResponseNormalizer

    def __post_init__(self) -> None:
        normalized = str(self.provider_id or "").strip().lower()
        if not normalized:
            raise ValueError("provider response hook id must be non-empty")
        if not callable(self.normalize_updates):
            raise TypeError("provider response normalizer must be callable")
        object.__setattr__(self, "provider_id", normalized)


@dataclass(frozen=True)
class ProviderResponseHookRegistry:
    """Immutable provider-to-response-normalizer registry.

    Request model hooks remain exact-model and runtime-root configurable.  This
    registry is deliberately smaller: it owns built-in parsers for provider
    response protocols that escaped through an otherwise standard wire shape.
    """

    hooks: Mapping[str, ProviderResponseHook]

    def __post_init__(self) -> None:
        normalized: dict[str, ProviderResponseHook] = {}
        for provider_id, hook in dict(self.hooks).items():
            key = str(provider_id or "").strip().lower()
            if not key or key != hook.provider_id:
                raise ValueError("provider response hook key must match hook.provider_id")
            if key in normalized:
                raise ValueError(f"duplicate provider response hook: {key}")
            normalized[key] = hook
        object.__setattr__(self, "hooks", MappingProxyType(normalized))

    @classmethod
    def builtin(cls) -> "ProviderResponseHookRegistry":
        from pal.llm.deepseek_response import normalize_deepseek_updates

        hook = ProviderResponseHook(
            provider_id="deepseek",
            normalize_updates=normalize_deepseek_updates,
        )
        return cls({hook.provider_id: hook})

    def normalize(
        self,
        *,
        endpoint_id: str,
        provider_id: str,
        model_id: str,
        wire_shape: WireShape,
        request: LLMRequestIR,
        updates: Iterable[LLMResponseUpdate],
    ) -> Iterator[LLMResponseUpdate]:
        hook = self.hooks.get(str(provider_id or "").strip().lower())
        if hook is None:
            yield from updates
            return
        context = ProviderResponseHookContext(
            endpoint_id=str(endpoint_id),
            provider_id=hook.provider_id,
            model_id=str(model_id),
            wire_shape=WireShape(wire_shape),
            request=request,
        )
        yield from hook.normalize_updates(context, updates)
