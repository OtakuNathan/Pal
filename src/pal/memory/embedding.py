from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL_NAME = "bge-m3"
DEFAULT_OLLAMA_KEEP_ALIVE = "5m"
DEFAULT_OLLAMA_REMOTE_TIMEOUT_SECONDS = 8.0
DEFAULT_OLLAMA_LOCAL_TIMEOUT_SECONDS = 120.0
DEFAULT_OLLAMA_FALLBACK_COOLDOWN_SECONDS = 3600.0
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
            with build_opener(ProxyHandler({})).open(request, timeout=max(float(self.timeout_seconds), 1.0)) as response:  # noqa: S310
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
class OllamaFallbackEmbeddingProvider:
    provider_id: str
    model_name: str
    providers: tuple[EmbeddingProviderPort, ...]
    retry_cooldown_seconds: float = DEFAULT_OLLAMA_FALLBACK_COOLDOWN_SECONDS
    transport: str = "ollama-fallback"
    _failed_until: dict[int, float] = field(default_factory=dict)
    _last_provider_index: int | None = None
    _last_errors: list[str] = field(default_factory=list)

    def embed_query(self, text: str) -> list[float]:
        return self._call("embed_query", text)

    def embed_document(self, text: str) -> list[float]:
        return self._call("embed_document", text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._call("embed_documents", texts)

    def health(self) -> dict[str, Any]:
        provider_payloads: list[dict[str, Any]] = []
        usable_index: int | None = None
        now = time.monotonic()
        for index, provider in enumerate(self.providers):
            failed_until = self._failed_until.get(id(provider), 0.0)
            if failed_until > now:
                payload = {
                    "healthy": False,
                    "provider_id": getattr(provider, "provider_id", f"provider_{index}"),
                    "transport": getattr(provider, "transport", "unknown"),
                    "model_name": getattr(provider, "model_name", self.model_name),
                    "base_url": getattr(provider, "base_url", ""),
                    "last_error": "provider cooling down after a previous embedding failure",
                }
            else:
                try:
                    payload = dict(provider.health())
                except Exception as exc:
                    payload = {
                        "healthy": False,
                        "provider_id": getattr(provider, "provider_id", f"provider_{index}"),
                        "transport": getattr(provider, "transport", "unknown"),
                        "model_name": getattr(provider, "model_name", self.model_name),
                        "last_error": str(exc),
                    }
            payload["priority"] = index
            payload["cooling_down"] = failed_until > now
            payload["cooldown_remaining_seconds"] = max(0.0, failed_until - now)
            provider_payloads.append(payload)
            if usable_index is None and bool(payload.get("healthy")) and payload.get("model_available", True) is not False:
                usable_index = index

        active_index = self._last_provider_index if self._last_provider_index is not None else usable_index
        active_provider = self.providers[active_index] if active_index is not None and 0 <= active_index < len(self.providers) else None
        healthy = usable_index is not None
        errors = [
            str(item.get("last_error") or "").strip()
            for item in provider_payloads
            if str(item.get("last_error") or "").strip()
        ]
        return {
            "healthy": healthy,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "model_name": self.model_name,
            "active_provider_id": getattr(active_provider, "provider_id", "") if active_provider is not None else "",
            "active_base_url": getattr(active_provider, "base_url", "") if active_provider is not None else "",
            "provider_count": len(self.providers),
            "providers": provider_payloads,
            "last_error": "" if healthy else "; ".join(errors or self._last_errors),
        }

    def _call(self, method_name: str, *args: Any) -> Any:
        providers = self._candidate_providers()
        errors: list[str] = []
        for index, provider in providers:
            try:
                method = getattr(provider, method_name)
                result = method(*args)
            except Exception as exc:
                self._failed_until[id(provider)] = time.monotonic() + max(float(self.retry_cooldown_seconds), 0.0)
                errors.append(f"{self._provider_label(provider)}: {exc}")
                continue
            self._failed_until.pop(id(provider), None)
            self._last_provider_index = index
            self._last_errors = []
            return result
        self._last_errors = errors
        raise RuntimeError(f"all embedding providers failed: {'; '.join(errors) if errors else 'no providers configured'}")

    def _candidate_providers(self) -> list[tuple[int, EmbeddingProviderPort]]:
        now = time.monotonic()
        active: list[tuple[int, EmbeddingProviderPort]] = []
        cooled_down: list[tuple[int, EmbeddingProviderPort]] = []
        for index, provider in enumerate(self.providers):
            if self._failed_until.get(id(provider), 0.0) > now:
                cooled_down.append((index, provider))
            else:
                active.append((index, provider))
        return active or cooled_down

    def _provider_label(self, provider: EmbeddingProviderPort) -> str:
        provider_id = str(getattr(provider, "provider_id", "") or "embedding_provider")
        base_url = str(getattr(provider, "base_url", "") or "").strip()
        return f"{provider_id}@{base_url}" if base_url else provider_id


def build_ollama_embedding_provider_from_config(config: Any | None = None) -> EmbeddingProviderPort:
    model_name = str(getattr(config, "embedding_ollama_model_name", DEFAULT_OLLAMA_MODEL_NAME) or DEFAULT_OLLAMA_MODEL_NAME)
    keep_alive = getattr(config, "embedding_ollama_keep_alive", DEFAULT_OLLAMA_KEEP_ALIVE)
    keep_alive = str(keep_alive) if keep_alive not in (None, "") else None
    local_base_url = _normalize_base_url(str(getattr(config, "embedding_ollama_local_base_url", DEFAULT_OLLAMA_BASE_URL) or DEFAULT_OLLAMA_BASE_URL))
    remote_base_urls = _coerce_base_urls(getattr(config, "embedding_ollama_remote_base_urls", ()))
    remote_timeout = _coerce_float(
        getattr(config, "embedding_ollama_remote_timeout_seconds", DEFAULT_OLLAMA_REMOTE_TIMEOUT_SECONDS),
        DEFAULT_OLLAMA_REMOTE_TIMEOUT_SECONDS,
    )
    local_timeout = _coerce_float(
        getattr(config, "embedding_ollama_local_timeout_seconds", DEFAULT_OLLAMA_LOCAL_TIMEOUT_SECONDS),
        DEFAULT_OLLAMA_LOCAL_TIMEOUT_SECONDS,
    )
    cooldown = _coerce_float(
        getattr(config, "embedding_ollama_fallback_cooldown_seconds", DEFAULT_OLLAMA_FALLBACK_COOLDOWN_SECONDS),
        DEFAULT_OLLAMA_FALLBACK_COOLDOWN_SECONDS,
    )

    local_provider = OllamaEmbeddingProvider(
        provider_id=DEFAULT_OLLAMA_PROVIDER_ID,
        model_name=model_name,
        base_url=local_base_url,
        keep_alive=keep_alive,
        timeout_seconds=local_timeout,
    )
    if not remote_base_urls:
        return local_provider

    providers: list[EmbeddingProviderPort] = []
    for index, base_url in enumerate(remote_base_urls, 1):
        providers.append(
            OllamaEmbeddingProvider(
                provider_id=f"ollama_remote_embedding_{index}",
                model_name=model_name,
                base_url=base_url,
                keep_alive=keep_alive,
                timeout_seconds=remote_timeout,
            )
        )
    providers.append(local_provider)
    return OllamaFallbackEmbeddingProvider(
        provider_id=DEFAULT_OLLAMA_PROVIDER_ID,
        model_name=model_name,
        providers=tuple(providers),
        retry_cooldown_seconds=cooldown,
    )


def _coerce_base_urls(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        url = _normalize_base_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return tuple(normalized)


def _normalize_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    return url


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


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
