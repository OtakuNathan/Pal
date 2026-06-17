from __future__ import annotations

from typing import Any

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import LLMProviderAdapter, LiteLLMCompletionDraft, _capabilities
from pal.llm.request_hooks import is_zai_glm_endpoint


class ZaiGLMProvider(LLMProviderAdapter):
    provider_names = frozenset({"zai", "zhipu"})
    adapter_names = frozenset({"glm", "zai", "zai_glm", "zhipu"})
    litellm_provider = "zai"
    model_provider_aliases = frozenset({"openai", "zai", "zhipu"})

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return is_zai_glm_endpoint(endpoint)

    def apply_request(self, request: CanonicalLLMRequest, draft: LiteLLMCompletionDraft) -> None:
        if not _supports_glm_openai_shape_thinking(self.endpoint):
            return
        thinking = _think_level_to_glm_openai_shape_thinking(request.metadata.get("think_level"))
        if thinking is None:
            return
        draft.thinking = thinking
        effort = _think_level_to_glm_reasoning_effort(request.metadata.get("think_level"))
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
    if text == "off":
        return {"type": "disabled"}
    return {"type": "enabled"}


def _think_level_to_glm_reasoning_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text or text == "off":
        return None
    return text
