"""Model request hooks and compaction policy, bound once per endpoint generation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import LLMRequestIR
from pal.llm.model_hooks import ModelHook, ModelHookRegistry
from pal.llm.response_hook_contracts import BoundResponseHooks
from pal.llm.response_hooks import ProviderResponseHookRegistry

COMPACTION_STRATEGIES = ("auto", "semantic_checkpoint", "openai_responses")
NATIVE_MODELS = frozenset({
    "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra",
})


@dataclass(frozen=True)
class ModelAdapter(ModelHook):
    """Export as MODEL_ADAPTER from the existing llm/models directory."""


@dataclass(frozen=True)
class BoundModelAdapter:
    spec: LLMEndpointSpec
    hook: ModelHook
    response_hooks: BoundResponseHooks
    compaction_strategy: str
    response_revision: str = "builtin-v1"

    @property
    def identity(self) -> str:
        return f"model-adapter.v1:{self.hook.revision}:{self.response_revision}:{self.compaction_strategy}"

    def apply(self, request: LLMRequestIR) -> LLMRequestIR:
        return self.hook.apply(request)

    def compact_payload(self, encoded: Mapping[str, Any]) -> dict[str, Any]:
        if self.compaction_strategy != "openai_responses":
            raise ValueError("endpoint uses semantic checkpoint compaction")
        # The standalone API has a separate schema. In particular, instructions
        # here change preserved-reasoning behavior on newer models.
        return {"model": self.spec.model_id, "input": encoded["input"]}


def supports_native_compaction(spec: LLMEndpointSpec) -> bool:
    return (spec.model_id in NATIVE_MODELS
            and spec.wire_shape == "openai_response"
            and spec.transport == "http"
            and spec.base_url.rstrip("/") == "https://api.openai.com/v1"
            and spec.auth_kind == "api_key_ref"
            and not spec.capabilities_blob.get("access_profile"))


def bind_model_adapter(
    spec: LLMEndpointSpec, models: ModelHookRegistry | None = None,
    responses: ProviderResponseHookRegistry | None = None,
) -> BoundModelAdapter:
    hook = models.hooks.get(spec.model_id) if models is not None else None
    hook = hook or ModelAdapter(spec.model_id)
    strategy = spec.capabilities_blob.get("compaction_strategy", hook.compaction_strategy)
    if strategy not in COMPACTION_STRATEGIES:
        raise ValueError(f"invalid compaction strategy: {strategy}")
    supported = supports_native_compaction(spec)
    if strategy == "auto":
        strategy = "openai_responses" if supported else "semantic_checkpoint"
    if strategy == "openai_responses" and not supported:
        raise ValueError(f"{spec.endpoint_id}: native compaction requires a verified official OpenAI HTTP Responses model")
    registry = responses or ProviderResponseHookRegistry.builtin()
    return BoundModelAdapter(spec, hook, registry.bind(
        endpoint_id=spec.endpoint_id, provider_id=spec.provider), str(strategy), registry.revision)
