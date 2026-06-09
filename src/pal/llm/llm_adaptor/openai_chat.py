from __future__ import annotations

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    LITELLM_RESPONSES_SHAPE,
    LLMProviderAdapter,
    LiteLLMCompletionDraft,
    _capabilities,
    _think_level_to_completion_reasoning_effort,
)
from pal.llm.llm_adaptor.openai_responses import OpenAIResponsesDraft, chat_messages_to_responses_input


class OpenAIChatProvider(LLMProviderAdapter):
    provider_names = frozenset({"openai_compatible"})
    adapter_names = frozenset({"openai_chat"})
    litellm_provider = "openai"
    model_provider_aliases = frozenset({"openai"})


class CodexBridgeProvider(LLMProviderAdapter):
    provider_names = frozenset({"codex_bridge"})
    adapter_names = frozenset({"codex", "codex_bridge"})
    litellm_provider = "openai"
    model_provider_aliases = frozenset({"openai", "hosted_vllm", "lm_studio", "llamafile"})
    request_shape = LITELLM_RESPONSES_SHAPE

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        capabilities = _capabilities(endpoint)
        return bool(capabilities.get("codex_bridge"))

    def new_draft(self, messages: list[dict[str, object]]) -> OpenAIResponsesDraft:  # type: ignore[override]
        instructions, input_items = chat_messages_to_responses_input(messages)  # type: ignore[arg-type]
        return OpenAIResponsesDraft(
            model=self.litellm_model(),
            instructions=instructions,
            input=input_items,
        )

    def apply_request(self, request: CanonicalLLMRequest, draft: LiteLLMCompletionDraft) -> None:
        effort = _think_level_to_completion_reasoning_effort(request.metadata.get("think_level"))
        if isinstance(draft, OpenAIResponsesDraft):
            if effort is not None:
                draft.reasoning = {"effort": effort}
            return
        draft.reasoning_effort = effort
