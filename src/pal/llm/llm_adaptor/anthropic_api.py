from __future__ import annotations

from typing import Any

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import LLMProviderAdapter, LiteLLMCompletionDraft


class AnthropicMessagesProvider(LLMProviderAdapter):
    provider_names = frozenset({"anthropic"})
    adapter_names = frozenset({"anthropic", "anthropic_messages"})
    litellm_provider = "anthropic"
    model_provider_aliases = frozenset({"anthropic"})

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return str(endpoint.api_mode or "") == "anthropic_messages"

    def apply_request(self, request: CanonicalLLMRequest, draft: LiteLLMCompletionDraft) -> None:
        draft.thinking = _think_level_to_anthropic_thinking(
            request.metadata.get("think_level"),
            request.max_output_tokens,
        )


def _think_level_to_anthropic_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "off": None,
        "minimal": "low",
        "low": "low",
        "balanced": "medium",
        "medium": "medium",
        "deep": "high",
        "high": "high",
        "xhigh": "high",
    }
    return mapping.get(text, "medium" if text else None)


def _think_level_to_anthropic_thinking(value: Any, max_output_tokens: int | None) -> dict[str, Any] | None:
    effort = _think_level_to_anthropic_effort(value)
    if effort is None:
        return None
    budget_map = {"low": 1024, "medium": 8192, "high": 32768}
    budget = budget_map.get(effort, 8192)
    if max_output_tokens is not None and budget >= max_output_tokens:
        budget = max(1024, max_output_tokens // 2)
    return {"type": "enabled", "budget_tokens": budget}
