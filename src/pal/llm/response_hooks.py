"""Provider defaults and endpoint differences bound during endpoint assembly."""
from __future__ import annotations

import importlib.util
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from pal.llm.ir import LLMRequestIR, LLMResponseUpdate, WireShape
from pal.llm.response_blocks import decode_response_blocks, updates_to_blocks
from pal.llm.response_hook_contracts import (
    BoundResponseHooks, ProviderResponseHookContext, ProviderResponseHookError, ResponseNormalizer,
)


@dataclass(frozen=True)
class ProviderResponseHook:
    provider_id: str
    normalize_updates: ResponseNormalizer

    def __post_init__(self) -> None:
        key = str(self.provider_id or "").strip().lower()
        if not key or not callable(self.normalize_updates):
            raise ValueError("provider hook requires an identity and callable normalizer")
        object.__setattr__(self, "provider_id", key)


@dataclass(frozen=True)
class EndpointResponseHook:
    endpoint_id: str
    normalize_updates: ResponseNormalizer

    def __post_init__(self) -> None:
        key = str(self.endpoint_id or "").strip()
        if not key or not callable(self.normalize_updates):
            raise ValueError("endpoint hook requires an identity and callable normalizer")
        object.__setattr__(self, "endpoint_id", key)


@dataclass(frozen=True)
class ProviderResponseHookRegistry:
    hooks: Mapping[str, ProviderResponseHook]
    endpoint_hooks: Mapping[str, EndpointResponseHook] = field(default_factory=dict)

    def __post_init__(self) -> None:
        providers = {str(key).strip().lower(): hook for key, hook in self.hooks.items()}
        if len(providers) != len(self.hooks) or any(key != hook.provider_id for key, hook in providers.items()):
            raise ValueError("provider hook keys must be unique and match their identities")
        endpoints = dict(self.endpoint_hooks)
        if any(key != hook.endpoint_id for key, hook in endpoints.items()):
            raise ValueError("endpoint hook key must match its identity")
        object.__setattr__(self, "hooks", MappingProxyType(providers))
        object.__setattr__(self, "endpoint_hooks", MappingProxyType(endpoints))

    @classmethod
    def builtin(cls) -> "ProviderResponseHookRegistry":
        from pal.llm.deepseek_response import normalize_deepseek_blocks

        hook = ProviderResponseHook("deepseek", normalize_deepseek_blocks)
        return cls({hook.provider_id: hook})

    @classmethod
    def load(
        cls, runtime_root: Path, *, defaults: "ProviderResponseHookRegistry | None" = None,
    ) -> "ProviderResponseHookRegistry":
        base = defaults if defaults is not None else cls.builtin()
        providers, endpoints = dict(base.hooks), dict(base.endpoint_hooks)
        providers.update({key: ProviderResponseHook(key, normalizer) for key, normalizer in
                          _load_hook_files(Path(runtime_root) / "llm/providers", "PROVIDER_ID", provider=True).items()})
        endpoints.update({key: EndpointResponseHook(key, normalizer) for key, normalizer in
                          _load_hook_files(Path(runtime_root) / "llm/endpoints", "ENDPOINT_ID").items()})
        return cls(providers, endpoints)

    def bind(self, *, endpoint_id: str, provider_id: str) -> BoundResponseHooks:
        provider = self.hooks.get(str(provider_id).strip().lower())
        endpoint = self.endpoint_hooks.get(str(endpoint_id))
        return BoundResponseHooks(
            provider.normalize_updates if provider is not None else None,
            endpoint.normalize_updates if endpoint is not None else None,
            str(provider_id).strip().lower(), str(endpoint_id),
        )

    def normalize(
        self, *, endpoint_id: str, provider_id: str, model_id: str,
        wire_shape: WireShape, request: LLMRequestIR, updates: Iterable[LLMResponseUpdate],
    ) -> Iterator[LLMResponseUpdate]:
        """Explicit convenience conversion for semantic fixtures and callers.

        Runtime invocation and continuation never re-enter this facade: the
        invoker contract already guarantees adapted, validated semantic IR.
        """
        bound = self.bind(endpoint_id=endpoint_id, provider_id=provider_id)
        if bound.provider is None and bound.endpoint is None:
            yield from updates
            return
        context = ProviderResponseHookContext(endpoint_id, provider_id, model_id, wire_shape, request)
        yield from decode_response_blocks(bound.normalize_blocks(context, updates_to_blocks(updates)))


def _load_hook_files(
    root: Path, identity_key: str, *, provider: bool = False,
) -> dict[str, ResponseNormalizer]:
    loaded: dict[str, ResponseNormalizer] = {}
    if not root.is_dir():
        return loaded
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"pal_response_hook_{identity_key}_{abs(hash(path.resolve()))}", path,
        )
        if spec is None or spec.loader is None:
            raise ProviderResponseHookError(f"cannot load response hook: {path}")
        module = importlib.util.module_from_spec(spec)
        try:
            # Reload edits even if size and filesystem timestamp match cached bytecode.
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        except Exception as exc:
            raise ProviderResponseHookError(f"response hook import failed: {path}") from exc
        identity = str(module.__dict__.get(identity_key) or "").strip()
        if provider:
            identity = identity.lower()
        normalizer = module.__dict__.get("normalize_blocks")
        if not identity or not callable(normalizer):
            raise ProviderResponseHookError(
                f"response hook must export {identity_key} and normalize_blocks: {path}"
            )
        if identity in loaded:
            raise ProviderResponseHookError(f"duplicate response hook for {identity}: {path}")
        loaded[identity] = normalizer
    return loaded
