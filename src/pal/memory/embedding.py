from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass
from typing import Protocol


class EmbeddingRuntimePort(Protocol):
    model_name: str

    def embed_query(self, text: str) -> list[float]:
        ...

    def embed_document(self, text: str) -> list[float]:
        ...


def normalize_vector(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        return [0.0 for _ in values]
    return [value / norm for value in values]


@dataclass
class SentenceTransformerBGEEmbedder:
    model_name: str = "BAAI/bge-m3"
    _model: object | None = None

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as exc:  # pragma: no cover - depends on optional package
                raise RuntimeError("sentence-transformers is not installed") from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _encode(self, text: str) -> list[float]:
        model = self._load_model()
        vector = model.encode(text or "", normalize_embeddings=True)
        return [float(value) for value in vector.tolist()]

    def embed_query(self, text: str) -> list[float]:
        return self._encode(text)

    def embed_document(self, text: str) -> list[float]:
        return self._encode(text)


@dataclass
class HashingEmbedder:
    model_name: str = "hashing-test-embedder"
    dimension: int = 32

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
