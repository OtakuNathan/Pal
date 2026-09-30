"""Provider normalization contracts independent of registry assembly."""
from __future__ import annotations

from dataclasses import dataclass

from pal.llm.ir import LLMRequestIR, WireShape


class ProviderResponseHookError(RuntimeError):
    """A provider response could not be normalized into Pal's LLM IR."""


@dataclass(frozen=True)
class ProviderResponseHookContext:
    endpoint_id: str
    provider_id: str
    model_id: str
    wire_shape: WireShape
    request: LLMRequestIR
