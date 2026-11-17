"""Durable constructors for the first-party embedding implementations."""
from __future__ import annotations

from typing import Any

from pal.memory.embedding import (
    EmbeddingProviderPort, HashingEmbedder, InProcBGEEmbeddingProvider,
    OllamaEmbeddingProvider, OllamaFallbackEmbeddingProvider,
)


def embedding_configuration(provider: EmbeddingProviderPort) -> dict[str, Any] | None:
    common = {"provider_id": provider.provider_id, "model_name": provider.model_name,
              "transport": provider.transport}
    if isinstance(provider, OllamaEmbeddingProvider):
        return {"kind": "ollama", "parameters": {**common, "base_url": provider.base_url,
            "keep_alive": provider.keep_alive, "timeout_seconds": provider.timeout_seconds,
            "model_revision": provider.model_revision, "text_processing_version": provider.text_processing_version}}
    if isinstance(provider, OllamaFallbackEmbeddingProvider):
        children = [embedding_configuration(child) for child in provider.providers]
        if any(child is None for child in children):
            return None
        return {"kind": "ollama_fallback", "parameters": {**common,
            "retry_cooldown_seconds": provider.retry_cooldown_seconds,
            "model_revision": provider.model_revision, "text_processing_version": provider.text_processing_version},
            "providers": children}
    if isinstance(provider, HashingEmbedder):
        return {"kind": "hashing", "parameters": {**common, "dimension": provider.dimension}}
    if isinstance(provider, InProcBGEEmbeddingProvider):
        return {"kind": "inproc_bge", "parameters": common}
    return None


def embedding_from_configuration(configuration: dict[str, Any]) -> EmbeddingProviderPort:
    parameters = configuration["parameters"]
    kind = configuration["kind"]
    if kind == "ollama":
        return OllamaEmbeddingProvider(**parameters)
    if kind == "ollama_fallback":
        return OllamaFallbackEmbeddingProvider(**parameters, providers=tuple(
            embedding_from_configuration(child) for child in configuration["providers"]))
    if kind == "hashing":
        return HashingEmbedder(**parameters)
    if kind == "inproc_bge":
        return InProcBGEEmbeddingProvider(**parameters)
    raise ValueError(f"unsupported snapshot embedding implementation: {kind}")
