from __future__ import annotations

import os
from dataclasses import dataclass, field

from pal.llm.models import LLMEndpointModel
from pal.llm.secret_store import KeyringSecretStore, SecretRef, SecretStorePort


def default_env_var_for_endpoint(endpoint: LLMEndpointModel) -> str | None:
    if endpoint.api_mode == "anthropic_messages" or endpoint.provider.lower() == "anthropic":
        return "ANTHROPIC_API_KEY"
    if endpoint.api_mode == "openai_chat" or endpoint.provider.lower() == "openai":
        return "OPENAI_API_KEY"
    return None


@dataclass
class LiteLLMCredentialResolver:
    """Resolve provider credentials without exposing them through introspection."""

    secret_store: SecretStorePort | None = None
    _cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.secret_store is None:
            self.secret_store = KeyringSecretStore()

    def resolve_api_key(self, endpoint: LLMEndpointModel) -> str | None:
        if endpoint.auth_kind == "local_provider_auth":
            return None

        cache_key = endpoint.endpoint_id
        if cache_key in self._cache:
            return self._cache[cache_key]

        for candidate in self._candidate_env_vars(endpoint):
            value = os.getenv(candidate)
            if value:
                self._cache[cache_key] = value
                return value

        secret = self._get_from_keyring(endpoint)
        if secret:
            self._cache[cache_key] = secret
            return secret
        return None

    def _candidate_env_vars(self, endpoint: LLMEndpointModel) -> list[str]:
        candidates: list[str] = []
        credential_ref = str(endpoint.credential_ref or "").strip()
        if credential_ref:
            candidates.append(credential_ref)
        default_env = default_env_var_for_endpoint(endpoint)
        if default_env and default_env not in candidates:
            candidates.append(default_env)
        return candidates

    def _get_from_keyring(self, endpoint: LLMEndpointModel) -> str | None:
        secret_ref = self.secret_ref_for_endpoint(endpoint)
        if secret_ref is None or self.secret_store is None:
            return None
        return self.secret_store.get_secret(secret_ref)

    def secret_ref_for_endpoint(self, endpoint: LLMEndpointModel) -> SecretRef | None:
        credential_ref = str(endpoint.credential_ref or "").strip()
        if not credential_ref:
            return None
        return SecretRef(service=credential_ref, account="api-key")
