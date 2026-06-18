from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from pal.llm.models import LLMEndpointModel
from pal.llm.secret_store import KeyringSecretStore, SecretRef, SecretStorePort


def default_env_var_for_endpoint(endpoint: LLMEndpointModel) -> str | None:
    if endpoint.api_mode == "anthropic_messages" or endpoint.provider.lower() == "anthropic":
        return "ANTHROPIC_API_KEY"
    if endpoint.api_mode == "openai_chat" or endpoint.provider.lower() == "openai":
        return "OPENAI_API_KEY"
    return None


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
            raw = self._get_from_secret_ref(secret_ref)
            if not raw:
                return ResolvedLLMAuth(kind="oauth", secret_ref=secret_ref)
            profile = _parse_oauth_profile(raw)
            access_token = raw.strip() if profile is None else _access_token_from_profile(profile)
            return ResolvedLLMAuth(
                kind="oauth",
                secret_ref=secret_ref,
                access_token=access_token,
                profile=profile or {},
            )
        return ResolvedLLMAuth(kind="api_key_ref", secret_ref=secret_ref, api_key=self.resolve_api_key(endpoint))

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


def _parse_oauth_profile(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _access_token_from_profile(profile: dict[str, Any]) -> str | None:
    for key in ("access_token", "token", "bearer_token"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
