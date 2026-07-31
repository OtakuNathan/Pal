from __future__ import annotations

from typing import Any

from pal.llm.contracts import (
    CanonicalLLMRequest,
    ThinkingChoice,
    ThinkingContract,
)
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    LLMProviderAdapter,
    OpenAIChatCompletionDraft,
    _capabilities,
    _normalize_key,
)


DEEPSEEK_THINKING_CONTRACT = ThinkingContract(
    choices=(
        ThinkingChoice("off", "off", aliases=("none",)),
        ThinkingChoice(
            "high",
            "high",
            aliases=("minimal", "low", "balanced", "medium", "deep"),
        ),
        ThinkingChoice("max", "max", aliases=("xhigh", "maximum")),
    ),
    default_choice_id="high",
)


class DeepSeekProvider(LLMProviderAdapter):
    provider_names = frozenset({"deepseek"})
    adapter_names = frozenset({"deepseek"})
    model_provider_prefix = "deepseek"
    model_provider_aliases = frozenset({"openai", "deepseek"})
    reasoning_content_messages = True

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return _is_deepseek_identifier(endpoint)

    def provider_thinking_contract(self) -> ThinkingContract | None:
        if not _supports_deepseek_thinking(self.endpoint):
            return None
        return DEEPSEEK_THINKING_CONTRACT

    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIChatCompletionDraft) -> None:
        if not _supports_deepseek_thinking(self.endpoint):
            return
        _normalize_deepseek_tool_call_history(draft.messages)
        choice_id = self.resolve_think_level(request.metadata.get("think_level"))
        thinking = _think_level_to_deepseek_thinking(choice_id)
        if thinking is not None:
            draft.thinking = thinking
            if thinking["type"] == "enabled":
                # DeepSeek V4 thinking mode rejects tool_choice. Supplying the
                # tools without it preserves the provider's automatic choice.
                draft.tool_choice = None
        effort = _think_level_to_deepseek_reasoning_effort(choice_id)
        if effort is not None:
            draft.reasoning_effort = effort


def _normalize_deepseek_tool_call_history(messages: list[dict[str, Any]]) -> None:
    """Preserve the exact assistant shape DeepSeek V4 requires after tool calls."""

    for message in messages:
        if str(message.get("role") or "").strip() != "assistant":
            continue
        if not list(message.get("tool_calls") or []):
            continue
        content = message.get("content")
        if content is None:
            message["content"] = ""


def _supports_deepseek_thinking(endpoint: LLMEndpointModel) -> bool:
    capabilities = _capabilities(endpoint)
    if capabilities.get("supports_thinking") is False or capabilities.get("thinking") is False:
        return False
    return bool(
        capabilities.get("supports_thinking")
        or capabilities.get("thinking")
        or getattr(endpoint, "supports_reasoning", False)
        or _is_deepseek_identifier(endpoint)
    )


def _think_level_to_deepseek_thinking(value: Any) -> dict[str, str] | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text == "off":
        return {"type": "disabled"}
    return {"type": "enabled"}


def _think_level_to_deepseek_reasoning_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "off": None,
        "minimal": "low",
        "low": "low",
        "balanced": "high",
        "medium": "high",
        "deep": "high",
        "high": "high",
        "xhigh": "max",
        "max": "max",
    }
    return mapping.get(text, "high" if text else None)


def _is_deepseek_identifier(endpoint: LLMEndpointModel) -> bool:
    capabilities = _capabilities(endpoint)
    adapter = _normalize_key(capabilities.get("adapter") or capabilities.get("llm_adapter") or "")
    provider = _normalize_key(getattr(endpoint, "provider", "") or "")
    model_id = _normalize_key(getattr(endpoint, "model_id", "") or "")
    base_url = _normalize_key(getattr(endpoint, "base_url", "") or "")
    return bool(
        adapter == "deepseek"
        or provider == "deepseek"
        or model_id.startswith("deepseek-")
        or model_id.startswith("deepseek/")
        or "/deepseek-" in model_id
        or "deepseek.com" in base_url
    )
