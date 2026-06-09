from __future__ import annotations

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    LLMProviderAdapter,
    LiteLLMCompletionDraft,
    _capabilities,
    _think_level_to_completion_reasoning_effort,
)


class OpenAIChatProvider(LLMProviderAdapter):
    provider_names = frozenset({"openai", "openai_compatible"})
    adapter_names = frozenset({"openai", "openai_chat"})
    litellm_provider = "openai"
    model_provider_aliases = frozenset({"openai"})


class CodexBridgeProvider(LLMProviderAdapter):
    provider_names = frozenset({"codex_bridge"})
    adapter_names = frozenset({"codex", "codex_bridge"})
    litellm_provider = "hosted_vllm"
    model_provider_aliases = frozenset({"openai", "hosted_vllm", "lm_studio", "llamafile"})

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        capabilities = _capabilities(endpoint)
        return bool(capabilities.get("codex_bridge"))

    def apply_request(self, request: CanonicalLLMRequest, draft: LiteLLMCompletionDraft) -> None:
        draft.reasoning_effort = _think_level_to_completion_reasoning_effort(request.metadata.get("think_level"))
