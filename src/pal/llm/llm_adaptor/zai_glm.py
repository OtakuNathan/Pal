from __future__ import annotations

from typing import Any

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.models import LLMEndpointModel

from pal.llm.llm_adaptor.base import LLMProviderAdapter, LiteLLMCompletionDraft, _capabilities, _normalize_key


class ZaiGLMProvider(LLMProviderAdapter):
    provider_names = frozenset({"zai", "zhipu"})
    adapter_names = frozenset({"glm", "zai", "zai_glm", "zhipu"})
    litellm_provider = "zai"
    model_provider_aliases = frozenset({"openai", "zai", "zhipu"})

    @classmethod
    def matches_endpoint(cls, endpoint: LLMEndpointModel) -> bool:
        return _is_glm_identifier(endpoint)

    def apply_request(self, request: CanonicalLLMRequest, draft: LiteLLMCompletionDraft) -> None:
        if not _supports_glm_thinking(self.endpoint):
            return
        thinking = _think_level_to_glm_thinking(request.metadata.get("think_level"))
        if thinking is not None:
            draft.extra_body["thinking"] = thinking


def _supports_glm_thinking(endpoint: LLMEndpointModel) -> bool:
    capabilities = _capabilities(endpoint)
    if capabilities.get("supports_thinking") is False or capabilities.get("thinking") is False:
        return False
    return bool(
        capabilities.get("supports_thinking")
        or capabilities.get("thinking")
        or getattr(endpoint, "supports_reasoning", False)
        or _is_glm_identifier(endpoint)
    )


def _think_level_to_glm_thinking(value: Any) -> dict[str, str] | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text == "off":
        return {"type": "disabled"}
    return {"type": "enabled"}


def _is_glm_identifier(endpoint: LLMEndpointModel) -> bool:
    capabilities = _capabilities(endpoint)
    adapter = _normalize_key(capabilities.get("adapter") or capabilities.get("llm_adapter") or "")
    provider = _normalize_key(getattr(endpoint, "provider", "") or "")
    model_id = _normalize_key(getattr(endpoint, "model_id", "") or "")
    base_url = _normalize_key(getattr(endpoint, "base_url", "") or "")
    return bool(
        adapter in {"glm", "zai", "zai_glm", "zhipu"}
        or provider in {"zai", "zhipu"}
        or model_id.startswith("glm-")
        or "/glm-" in model_id
        or "bigmodel.cn" in base_url
        or "api.z.ai" in base_url
    )
