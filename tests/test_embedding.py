from __future__ import annotations

import unittest

from pal.core.runtime_config import RuntimeConfig
from pal.memory.embedding import (
    OllamaFallbackEmbeddingProvider,
    build_ollama_embedding_provider_from_config,
)


class _FailingEmbedder:
    provider_id = "remote"
    model_name = "bge-m3"
    transport = "test"
    base_url = "http://remote:11434"

    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        raise RuntimeError("remote-down")

    def embed_document(self, text: str) -> list[float]:
        self.calls += 1
        raise RuntimeError("remote-down")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise RuntimeError("remote-down")

    def health(self) -> dict[str, object]:
        self.calls += 1
        raise RuntimeError("remote-down")


class _WorkingEmbedder:
    provider_id = "local"
    model_name = "bge-m3"
    transport = "test"
    base_url = "http://local:11434"

    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_document(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]

    def health(self) -> dict[str, object]:
        return {
            "healthy": True,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "model_name": self.model_name,
            "model_available": True,
            "last_error": "",
        }


class EmbeddingProviderTests(unittest.TestCase):
    def test_build_ollama_embedding_provider_uses_remote_first_fallback_when_configured(self) -> None:
        provider = build_ollama_embedding_provider_from_config(
            RuntimeConfig(embedding_ollama_remote_base_urls=("192.168.31.145:11434",))
        )

        self.assertIsInstance(provider, OllamaFallbackEmbeddingProvider)
        assert isinstance(provider, OllamaFallbackEmbeddingProvider)
        self.assertEqual(provider.provider_id, "ollama_local_embedding")
        self.assertEqual([getattr(item, "base_url", "") for item in provider.providers], ["http://192.168.31.145:11434", "http://127.0.0.1:11434"])

    def test_fallback_provider_cools_down_failed_remote_and_uses_local(self) -> None:
        remote = _FailingEmbedder()
        local = _WorkingEmbedder()
        provider = OllamaFallbackEmbeddingProvider(
            provider_id="ollama_local_embedding",
            model_name="bge-m3",
            providers=(remote, local),
            retry_cooldown_seconds=60,
        )

        self.assertEqual(provider.embed_documents(["hello"]), [[1.0, 0.0]])
        self.assertEqual(provider.embed_documents(["again"]), [[1.0, 0.0]])
        health = provider.health()

        self.assertEqual(remote.calls, 1)
        self.assertEqual(local.calls, 2)
        self.assertTrue(health["healthy"])
        self.assertTrue(health["providers"][0]["cooling_down"])


if __name__ == "__main__":
    unittest.main()
