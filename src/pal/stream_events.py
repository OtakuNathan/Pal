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
    response_mode: str | None = None
    error_text: str = ""
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    preferred_endpoint_id: str | None = None
    preferred_model_id: str | None = None
