from __future__ import annotations

from pal.llm.contracts import CanonicalLLMRequest, ThinkingContract
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import (
    OPENAI_RESPONSES_SHAPE,
    LLMProviderAdapter,
    OpenAIChatCompletionDraft,
    _capabilities,
    _think_level_to_completion_reasoning_effort,
    openai_thinking_contract_for_endpoint,
)
from pal.llm.llm_adaptor.openai_responses import OpenAIResponsesDraft, chat_messages_to_responses_input


class OpenAIChatProvider(LLMProviderAdapter):
    provider_names = frozenset({"openai_compatible"})
    adapter_names = frozenset({"openai_chat"})
    model_provider_prefix = "openai"
    model_provider_aliases = frozenset({"openai"})


class CodexBridgeProvider(LLMProviderAdapter):
    provider_names = frozenset({"codex_bridge", "codex_cli", "codex_app_server"})
    adapter_names = frozenset({"codex", "codex_bridge", "codex_app_server"})
    model_provider_prefix = "openai"
    model_provider_aliases = frozenset({"openai", "hosted_vllm", "lm_studio", "llamafile"})
    request_shape = OPENAI_RESPONSES_SHAPE

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        capabilities = _capabilities(endpoint)
        return bool(
            capabilities.get("codex_bridge")
            or capabilities.get("codex_app_server")
            or capabilities.get("official_codex_app_server")
            or capabilities.get("official_codex_cli")
        )

    def provider_thinking_contract(self) -> ThinkingContract | None:
        return openai_thinking_contract_for_endpoint(self.endpoint)

    def new_draft(self, messages: list[dict[str, object]]) -> OpenAIResponsesDraft:  # type: ignore[override]
        instructions, input_items = chat_messages_to_responses_input(messages)  # type: ignore[arg-type]
        return OpenAIResponsesDraft(
            model=self.api_model(),
            instructions=instructions,
            input=input_items,
        )

    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIChatCompletionDraft) -> None:
        choice_id = self.resolve_think_level(request.metadata.get("think_level"))
        effort = _think_level_to_completion_reasoning_effort(choice_id)
        if isinstance(draft, OpenAIResponsesDraft):
            if effort is not None:
                draft.reasoning = {"effort": effort}
            return
        draft.reasoning_effort = effort
