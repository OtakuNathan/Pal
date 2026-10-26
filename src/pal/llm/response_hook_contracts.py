"""Bound protocol-adaptation contracts independent of registry assembly."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from pal.llm.ir import LLMRequestIR, WireShape
from pal.llm.response_blocks import ResponseBlockUpdate


class ProviderResponseHookError(RuntimeError):
    """A response protocol adapter could not normalize its response blocks."""


@dataclass(frozen=True)
class ProviderResponseHookContext:
    endpoint_id: str
    provider_id: str
    model_id: str
    wire_shape: WireShape
    request: LLMRequestIR


ResponseNormalizer = Callable[
    [ProviderResponseHookContext, Iterable[ResponseBlockUpdate]], Iterable[ResponseBlockUpdate],
]


@dataclass(frozen=True)
class BoundResponseHooks:
    """Immutable per-endpoint composition; each request gets fresh iterators."""

    provider: ResponseNormalizer | None = None
    endpoint: ResponseNormalizer | None = None
    provider_id: str = ""
    endpoint_id: str = ""

    def normalize_blocks(
        self, context: ProviderResponseHookContext, updates: Iterable[ResponseBlockUpdate],
    ) -> Iterator[ResponseBlockUpdate]:
        if ((self.provider_id and context.provider_id.strip().lower() != self.provider_id)
                or (self.endpoint_id and context.endpoint_id != self.endpoint_id)):
            raise ProviderResponseHookError("response-hook binding does not match request endpoint")
        normalized = self.provider(context, updates) if self.provider is not None else updates
        normalized = self.endpoint(context, normalized) if self.endpoint is not None else normalized
        yield from normalized
