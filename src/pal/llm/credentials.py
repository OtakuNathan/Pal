from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from pal.llm.models import LLMEndpointModel
from pal.llm.secret_store import KeyringSecretStore, SecretRef, SecretStorePort


class LLMCredentialUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedLLMAuth:
    kind: str
    secret_ref: SecretRef | None = None
    api_key: str | None = None
    access_token: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMCredentialResolver:
    """Resolve provider credentials without exposing them through introspection."""

    secret_store: SecretStorePort | None = None
    _cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.secret_store is None:
            self.secret_store = KeyringSecretStore()

    def refresh(self) -> None:
        self._cache.clear()
        secret_store_refresh = getattr(self.secret_store, "refresh", None)
        if callable(secret_store_refresh):
            secret_store_refresh()

    def clear_cache(self) -> None:
        self._cache.clear()

    def resolve_api_key(self, endpoint: LLMEndpointModel) -> str | None:
        if endpoint.auth_kind == "local_provider_auth":
            return None
        if endpoint.auth_kind == "oauth":
            auth = self.resolve_auth(endpoint)
            return auth.access_token

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

    def resolve_auth(self, endpoint: LLMEndpointModel) -> ResolvedLLMAuth:
        if endpoint.auth_kind == "local_provider_auth":
            return ResolvedLLMAuth(kind="local_provider_auth")
        secret_ref = self.secret_ref_for_endpoint(endpoint)
        if endpoint.auth_kind == "oauth":
            from pal.llm.chatgpt import ChatGPTAuthService, ChatGPTError, API_URL, is_chatgpt
            if secret_ref is None or not is_chatgpt(endpoint) or endpoint.base_url.rstrip("/") != API_URL:
                raise ChatGPTError("reauthorization_required")
            access_token = ChatGPTAuthService(self.secret_store).access_token(secret_ref)
            profile = {}  # Never expose renewable tokens in resolved auth metadata.
            return ResolvedLLMAuth(
                kind="oauth",
                secret_ref=secret_ref,
                access_token=access_token,
                profile=profile or {},
            )
        return ResolvedLLMAuth(kind="api_key_ref", secret_ref=secret_ref, api_key=self.resolve_api_key(endpoint))

    def _candidate_env_vars(self, endpoint: LLMEndpointModel) -> list[str]:
        credential_ref = str(endpoint.credential_ref or "").strip()
        return [credential_ref] if credential_ref else []

    def _get_from_keyring(self, endpoint: LLMEndpointModel) -> str | None:
        secret_ref = self.secret_ref_for_endpoint(endpoint)
        return self._get_from_secret_ref(secret_ref)

    def _get_from_secret_ref(self, secret_ref: SecretRef | None) -> str | None:
        if secret_ref is None or self.secret_store is None:
            return None
        return self.secret_store.get_secret(secret_ref)

    def secret_ref_for_endpoint(self, endpoint: LLMEndpointModel) -> SecretRef | None:
        credential_ref = str(endpoint.credential_ref or "").strip()
        if not credential_ref:
            return None
        default_account = "oauth-profile" if endpoint.auth_kind == "oauth" else "api-key"
        if ":" in credential_ref:
            service, account = credential_ref.split(":", 1)
            service = service.strip()
            account = account.strip() or default_account
            if service:
                return SecretRef(service=service, account=account)
        return SecretRef(service=credential_ref, account=default_account)
