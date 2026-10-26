"""Default endpoint composition, separate from runtime orchestration."""
from __future__ import annotations

from pathlib import Path

from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.invocation_contracts import ManagedEndpointInvokerPort
from pal.llm.response_hooks import ProviderResponseHookRegistry
from pal.llm.transport import DirectEndpointTransport


def build_default_endpoint_invoker(
    *, credentials: LLMCredentialResolver | None = None,
    runtime_root: str | Path | None = None,
    response_hooks: ProviderResponseHookRegistry | None = None,
) -> ManagedEndpointInvokerPort:
    resolver = credentials or LLMCredentialResolver()
    hooks = response_hooks if response_hooks is not None else ProviderResponseHookRegistry.load(
        Path(runtime_root or ".")
    )
    return ShapeEndpointInvoker(
        transport=DirectEndpointTransport(credential_resolver=resolver.resolve_api_key),
        response_hooks=hooks,
    )
