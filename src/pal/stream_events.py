from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pal.llm.contracts import CanonicalToolCall


@dataclass(frozen=True)
class NormalizedLLMStreamEvent:
    event_kind: str = "text_delta"
    text: str = ""
    reasoning_text: str = ""
    provider_specific_fields: dict[str, Any] = field(default_factory=dict)
    tool_call: CanonicalToolCall | None = None
    finish_reason: str | None = None
    input_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    usage_reported: bool = False
    provider_response_count: int = 0
    response_mode: str | None = None
    error_text: str = ""
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    preferred_endpoint_id: str | None = None
    preferred_model_id: str | None = None
