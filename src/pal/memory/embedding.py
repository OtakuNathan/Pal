from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL_NAME = "bge-m3"
DEFAULT_OLLAMA_KEEP_ALIVE = "5m"
DEFAULT_OLLAMA_PROVIDER_ID = "ollama_local_embedding"
INPROC_BGE_PROVIDER_ID = "inproc_bge_m3"
HASHING_PROVIDER_ID = "hashing_test_provider"


class EmbeddingProviderPort(Protocol):
    provider_id: str
    model_name: str
    transport: str

    def embed_query(self, text: str) -> list[float]:
        ...

    def embed_document(self, text: str) -> list[float]:
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    def health(self) -> dict[str, Any]:
        ...


EmbeddingRuntimePort = EmbeddingProviderPort


def normalize_vector(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        return [0.0 for _ in values]
    return [value / norm for value in values]


@dataclass
class OllamaEmbeddingProvider:
    provider_id: str = DEFAULT_OLLAMA_PROVIDER_ID
    model_name: str = DEFAULT_OLLAMA_MODEL_NAME
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    keep_alive: str | None = DEFAULT_OLLAMA_KEEP_ALIVE
    timeout_seconds: float = 120.0
    transport: str = "http"

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []

    def embed_document(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        normalized = [str(text or "") for text in texts]
        if not normalized:
            return []
        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": normalized,
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        response = self._request_json("POST", "/api/embed", payload)
        vectors = response.get("embeddings")
        if not isinstance(vectors, list):
            raise RuntimeError("ollama embedding response missing embeddings")
        decoded: list[list[float]] = []
        for value in vectors:
            if not isinstance(value, list):
                raise RuntimeError("ollama embedding response returned invalid vector payload")
            decoded.append([float(item) for item in value])
        return decoded

    def health(self) -> dict[str, Any]:
        try:
            payload = self._request_json("GET", "/api/tags", None)
        except Exception as exc:
            return {
                "healthy": False,
                "provider_id": self.provider_id,
                "transport": self.transport,
                "base_url": self.base_url,
                "model_name": self.model_name,
                "last_error": str(exc),
            }
        models = payload.get("models")
        installed_names: list[str] = []
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    installed_names.append(name)
        return {
            "healthy": True,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "model_available": any(self._model_matches(name) for name in installed_names),
            "installed_model_count": len(installed_names),
            "last_error": "",
        }

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = None
        headers = {"User-Agent": "PalV2/0.1"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url.rstrip('/')}{path}", data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=max(float(self.timeout_seconds), 1.0)) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"ollama request failed with {exc.code}: {body or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"ollama request failed: {exc.reason}") from exc
        decoded = json.loads(raw or "{}")
        if not isinstance(decoded, dict):
            raise RuntimeError("ollama returned invalid JSON payload")
        if decoded.get("error"):
            raise RuntimeError(str(decoded.get("error")))
        return decoded

    def _model_matches(self, installed_name: str) -> bool:
        requested = str(self.model_name or "").strip().lower()
        installed = str(installed_name or "").strip().lower()
        if not requested or not installed:
            return False
        if installed == requested:
            return True
        return f"{requested}:latest" == installed or requested == installed.removesuffix(":latest")


@dataclass
class InProcBGEEmbeddingProvider:
    provider_id: str = INPROC_BGE_PROVIDER_ID
    model_name: str = "BAAI/bge-m3"
    _model: object | None = None
    transport: str = "inproc"

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as exc:  # pragma: no cover - depends on optional package
                raise RuntimeError("sentence-transformers is not installed") from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _encode(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0] if vectors else []

    def embed_query(self, text: str) -> list[float]:
        return self._encode(text)

    def embed_document(self, text: str) -> list[float]:
        return self._encode(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        normalized = [str(text or "") for text in texts]
        if not normalized:
            return []
        model = self._load_model()
        vectors = model.encode(normalized, normalize_embeddings=True)
        rows = vectors.tolist() if hasattr(vectors, "tolist") else vectors
        return [[float(value) for value in row] for row in rows]

    def health(self) -> dict[str, Any]:
        loaded = self._model is not None
        return {
            "healthy": True,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "model_name": self.model_name,
            "model_loaded": loaded,
            "last_error": "",
        }


@dataclass
class HashingEmbedder:
    provider_id: str = HASHING_PROVIDER_ID
    model_name: str = "hashing-test-embedder"
    dimension: int = 32
    transport: str = "test"

    def _encode(self, text: str) -> list[float]:
        buckets = [0.0] * self.dimension
        tokens = Counter(token.lower() for token in str(text or "").split() if token.strip())
        if not tokens:
            return buckets
        for token, count in tokens.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dimension
            buckets[bucket] += float(count)
        return normalize_vector(buckets)

    def embed_query(self, text: str) -> list[float]:
        return self._encode(text)

    def embed_document(self, text: str) -> list[float]:
        return self._encode(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_document(text) for text in texts]

    def health(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "model_name": self.model_name,
            "dimension": self.dimension,
            "last_error": "",
        }


SentenceTransformerBGEEmbedder = InProcBGEEmbeddingProvider
