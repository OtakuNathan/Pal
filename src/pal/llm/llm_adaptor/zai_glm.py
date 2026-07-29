from __future__ import annotations

from typing import Any

from pal.llm.contracts import CanonicalLLMRequest, ThinkingChoice, ThinkingContract
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    ANTHROPIC_THINKING_CONTRACT,
    LLMProviderAdapter,
    OpenAIChatCompletionDraft,
    _capabilities,
)
from pal.llm.request_hooks import is_zai_glm_endpoint

GLM_OPENAI_THINKING_CONTRACT = ThinkingContract(
    choices=(
        ThinkingChoice("off", "off", aliases=("none", "minimal")),
        ThinkingChoice("high", "high", aliases=("low", "balanced", "medium", "deep")),
        ThinkingChoice("max", "max", aliases=("xhigh", "maximum")),
    ),
    default_choice_id="high",
)


class ZaiGLMProvider(LLMProviderAdapter):
    provider_names = frozenset({"zai", "zhipu"})
    adapter_names = frozenset({"glm", "zai", "zai_glm", "zhipu"})
    model_provider_prefix = "zai"
    model_provider_aliases = frozenset({"openai", "zai", "zhipu"})
    reasoning_content_messages = True

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return is_zai_glm_endpoint(endpoint)

    def provider_thinking_contract(self) -> ThinkingContract | None:
        if not _supports_glm_openai_shape_thinking(self.endpoint):
            return None
        if str(getattr(self.endpoint, "api_mode", "") or "").strip().lower() == "anthropic_messages":
            return ANTHROPIC_THINKING_CONTRACT
        return GLM_OPENAI_THINKING_CONTRACT

    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIChatCompletionDraft) -> None:
        if not _supports_glm_openai_shape_thinking(self.endpoint):
            return
        choice_id = self.resolve_think_level(request.metadata.get("think_level"))
        thinking = _think_level_to_glm_openai_shape_thinking(choice_id)
        if thinking is None:
            return
        draft.thinking = thinking
        effort = _think_level_to_glm_reasoning_effort(choice_id)
        if effort is not None:
            draft.reasoning_effort = effort


def _supports_glm_openai_shape_thinking(endpoint: LLMEndpointModel) -> bool:
    capabilities = _capabilities(endpoint)
    if capabilities.get("supports_thinking") is False or capabilities.get("thinking") is False:
        return False
    return bool(
        capabilities.get("supports_thinking")
        or capabilities.get("thinking")
        or getattr(endpoint, "supports_reasoning", False)
        or is_zai_glm_endpoint(endpoint)
    )


def _think_level_to_glm_openai_shape_thinking(value: Any) -> dict[str, str] | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"off", "none", "minimal"}:
        return {"type": "disabled"}
    return {"type": "enabled"}


def _think_level_to_glm_reasoning_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "off": None,
        "none": None,
        "high": "high",
        "max": "max",
    }
    return mapping.get(text)
