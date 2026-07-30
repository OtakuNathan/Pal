from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pal.bootstrap import compose_runtime
from pal.channel import ChannelEndpointRepository, ChannelRuntime, FactoryChannelProvider
from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.channel.endpoints import SocketChannelEndpoint
from pal.channel.endpoints.socket_protocol import pack_socket_message, read_socket_message
from pal.channel.capabilities import ChannelIntrospectionProvider
from pal.control import InteractionButtonSpec, InteractionMessageSpec
from pal.execution import CapabilityCall
from pal.core.runtime_config import RuntimeConfig
from pal.identity import DEFAULT_PERSONA_ID, IdentityRepository
from pal.llm import (
    AnthropicMessagesEndpointInvoker,
    CanonicalLLMRequest,
    CanonicalLLMOutcome,
    EncryptedFileSecretStore,
    EndpointResolver,
    InMemorySecretStore,
    LLMEndpointRepository,
    LLMEndpointInvocationError,
    LLMPreflightRequest,
    LLMRuntime,
    LLMCredentialResolver,
    OpenAIChatEndpointInvoker,
    RuntimeSettingRepository,
    SecretRef,
    ZaiAnthropicMessagesEndpointInvoker,
    build_default_endpoint_invoker,
)
from pal.llm.adapters import LLMProviderAdapter, LLMProviderRegistry, build_default_provider_registry
from pal.llm.llm_adaptor.anthropic_api import chat_messages_to_anthropic_messages
from pal.llm.llm_adaptor.base import chat_messages_to_openai_compatible_messages
from pal.llm.runtime import _parse_anthropic_messages_response, _split_openai_chat_sdk_kwargs
from pal.llm.request_hooks import MAIN_LLM_REQUEST_HOOKS
from pal.memory import HashingEmbedder, L3CommitRequest, L3CorrectRequest, L3ProviderSelector, MemoryPackRequest, MemoryQuery, MemoryService
from pal.plugins.l3 import SQLiteVecL3Plugin
from pal.plugins import PluginBundleRepository
from pal.proactive import ProactiveDefinition, ProactiveRepository
from pal.shared import LLMFinishReason, LLMStreamEventKind, SINGLETON_TARGET
from pal.stream_events import NormalizedLLMStreamEvent
from pal.wizard import WizardService
from pal.web_fetch import DEFAULT_WEB_FETCH_USER_AGENT, BrowserServiceManager, WebFetchProviderRepository, plain_http_fetch
from pal.web_search import WebSearchItem, WebSearchProviderRepository
from tests.runtime_channel_providers import telegram_endpoint_module


_telegram_module = telegram_endpoint_module()
TelegramChannelEndpoint = _telegram_module.TelegramChannelEndpoint
TelegramChannelEndpointFactory = _telegram_module.TelegramChannelEndpointFactory


def _unix_socket_bind_available() -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="pal_socket_probe_") as root:
            socket_path = Path(root) / "probe.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.bind(str(socket_path))
        return True
    except OSError:
        return False


def _tcp_loopback_bind_available() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


def _local_sidecar_bind_available() -> bool:
    return _unix_socket_bind_available() or _tcp_loopback_bind_available()


class _OutboundQueue:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def put_nowait(self, item: dict[str, object]) -> None:
        self.items.append(item)


class PalV2BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_bootstrap_test_"))
        self._runtime_handles = []
        self.wizard = WizardService()
        self.registration = self.wizard.provision_runtime(
            display_name="PalV2 Test",
            runtime_root=self.runtime_root,
            db_filename="pal_test.sqlite3",
            pal_entrypoint="pal.runtime.test",
        )
        self.db_path = self.registration.runtime.db_path
        self.database = self.wizard.create_database(self.registration)
        self.wizard.provision_builtin_plugins(self.registration)
        self.wizard.provision_runtime_channel_providers(self.registration)

    def tearDown(self) -> None:
        for handle in reversed(self._runtime_handles):
            with contextlib.suppress(Exception):
                asyncio.run(handle.stop_async())
        self.database.close()
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def _compose_runtime(self, **kwargs):
        handle = compose_runtime(**kwargs)
        self._runtime_handles.append(handle)
        return handle

    def _find_search_hit_by_alias(self, core, search, alias: str) -> dict:
        _ = core
        for item in search.structured["hits"]:
            if item.get("alias") == alias:
                return item
        self.fail(f"search result did not include alias {alias}")

    def _write_demo_runtime_channel_provider(self) -> None:
        provider_dir = self.runtime_root / "channel" / "providers" / "demo_runtime"
        provider_dir.mkdir(parents=True, exist_ok=True)
        (provider_dir / "provider.toml").write_text(
            "\n".join(
                [
                    'provider_id = "demo_runtime"',
                    'entrypoint = "runtime.py"',
                    'version = "0.1.0"',
                    "enabled = true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (provider_dir / "runtime.py").write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "from dataclasses import dataclass",
                    "from pathlib import Path",
                    "from typing import Any",
                    "from pal.channel import ChannelEndpointQueueBase, EndpointConfig, FactoryChannelProvider",
                    "from pal.channel.models import ChannelEndpointModel",
                    "",
                    "class DemoRuntimeEndpoint(ChannelEndpointQueueBase):",
                    "    def normalize_raw(self, payload: Any) -> dict[str, Any]:",
                    "        return {}",
                    "",
                    "    def send_reply(self, response_handle, text: str) -> None:",
                    "        _ = response_handle, text",
                    "",
                    "    def inspect_health(self) -> dict[str, Any]:",
                    "        return {'healthy': True, 'source': 'runtime_root_provider', 'provider_code_version': 1}",
                    "",
                    "    def inspect_auth_state(self) -> dict[str, Any]:",
                    "        return {'authorized': True, 'source': 'runtime_root_provider'}",
                    "",
                    "@dataclass(frozen=True)",
                    "class DemoRuntimeFactory:",
                    "    channel_kind: str = 'demo_runtime'",
                    "    reload_modules: tuple[str, ...] = ()",
                    "",
                    "    def create(self, record: ChannelEndpointModel, *, runtime_root: Path):",
                    "        _ = runtime_root",
                    "        endpoint = DemoRuntimeEndpoint(",
                    "            endpoint=EndpointConfig(",
                    "                endpoint_id=record.endpoint_id,",
                    "                channel_kind=record.channel_kind,",
                    "                binding_key=record.binding_key,",
                    "                send_policy=dict(record.send_policy_blob or {}),",
                    "            )",
                    "        )",
                    "        endpoint.enabled = bool(record.enabled)",
                    "        endpoint.attached = record.detached_at is None",
                    "        endpoint.paired = True",
                    "        return endpoint",
                    "",
                    "def build_channel_provider(context):",
                    "    return FactoryChannelProvider(",
                    "        provider_id=context.manifest.provider_id,",
                    "        endpoint_types=('demo_runtime',),",
                    "        factory=DemoRuntimeFactory(),",
                    "        reload_modules=('_pal_runtime_channel_provider_demo_runtime',),",
                    "    )",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def test_bootstrap_creates_only_new_tables(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertIn("pal_personas", tables)
        self.assertIn("channel_endpoints", tables)
        self.assertIn("llm_endpoints", tables)
        self.assertIn("pal_runtime_settings", tables)
        self.assertIn("plugin_bundles", tables)
        self.assertIn("proactive_definitions", tables)
        self.assertIn("proactive_runs", tables)
        self.assertIn("web_search_providers", tables)
        self.assertIn("web_fetch_providers", tables)
        self.assertNotIn("users", tables)
        self.assertNotIn("conversation_routes", tables)
        self.assertNotIn("pal_memories", tables)

    def test_compose_runtime_includes_proactive_runtime(self) -> None:
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        self.assertIsNotNone(handle.proactive_manager)
        self.assertIsNotNone(handle.proactive_repository)
        self.assertIsNotNone(handle.proactive_runner)
        self.assertIn("proactive", handle.core.context.module_registry.modules)

    def test_proactive_repository_round_trips_definition_and_run(self) -> None:
        repository = ProactiveRepository()
        definition = ProactiveDefinition(
            proactive_id="daily_digest",
            goal="Summarize repository updates",
            method="Review recent changes and publish a short digest.",
            skill_refs=["git", "summary"],
            out_channel_id="socket_default",
            out_reply_target={"session_id": "session-1", "request_id": "req-1"},
            schedule={"cadence": "daily", "hour": 9, "minute": 0, "timezone": "Asia/Shanghai"},
            enabled=True,
        )

        repository.upsert_definition(definition, next_due_at_utc="2026-04-11T01:00:00+00:00")
        stored = repository.list_definitions()

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].definition.proactive_id, "daily_digest")
        self.assertEqual(stored[0].definition.skill_refs, ["git", "summary"])
        self.assertEqual(stored[0].definition.out_reply_target, {"session_id": "session-1", "request_id": "req-1"})
        self.assertEqual(stored[0].next_due_at_utc, "2026-04-11T01:00:00+00:00")

    def test_wizard_provision_runtime_creates_third_party_plugin_directory(self) -> None:
        self.assertTrue((self.registration.runtime.runtime_root / "plugins").is_dir())

    def test_wizard_leaves_minion_catalog_ownership_to_the_sidecar(self) -> None:
        minion_plugin_root = self.registration.runtime.runtime_root / "plugins" / "minion"

        self.assertFalse((minion_plugin_root / "profiles").exists())
        self.assertFalse((minion_plugin_root / "families").exists())

    def test_identity_repository_bootstraps_singletons(self) -> None:
        repository = IdentityRepository()
        repository.ensure_defaults(
            display_name="Pal",
            language="zh",
            vibe="calm",
            tone="direct",
            core_policy=["Tool is the only execution primitive."],
            timezone="Asia/Shanghai",
        )

        persona = repository.get_persona()
        preferences = repository.get_user_preferences()

        self.assertIsNotNone(persona)
        self.assertEqual(persona.persona_id, DEFAULT_PERSONA_ID)
        self.assertEqual(persona.display_name, "Pal")
        self.assertEqual(persona.language, "zh")
        self.assertEqual(persona.core_policy, ["Tool is the only execution primitive."])

        self.assertIsNotNone(preferences)
        self.assertEqual(preferences.timezone, "Asia/Shanghai")
        self.assertEqual(preferences.preferences_blob, {})

    def test_identity_repository_refreshes_legacy_empty_defaults(self) -> None:
        repository = IdentityRepository()
        repository.ensure_defaults(display_name="pal", language="en")

        repository.ensure_defaults()

        persona = repository.get_persona()
        preferences = repository.get_user_preferences()

        self.assertIsNotNone(persona)
        self.assertEqual(persona.display_name, "Pal")
        self.assertIn("Default to the user's language", persona.language)
        self.assertEqual(persona.vibe, "thoughtful, direct, warm, humorous, non-preachy.")
        self.assertEqual(persona.tone, "direct, humorous")
        self.assertIn("Never fabricate facts, memory, or runtime state.", persona.core_policy)
        self.assertIsNotNone(preferences)
        self.assertIn("Be concise.", preferences.style_preference)
        self.assertEqual(preferences.timezone, "Asia/Shanghai")

    def test_channel_endpoint_repository_upserts_and_filters_enabled_endpoints(self) -> None:
        repository = ChannelEndpointRepository()
        repository.upsert(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat-123",
            enabled=True,
            supports_typing=True,
            supports_receipt_marker=True,
            binding_metadata={"chat_id": "123"},
            send_policy_blob={"chunk_size": 4096},
        )
        repository.upsert(
            endpoint_id="socket_local",
            channel_kind="socket",
            binding_key="/tmp/pal.sock",
            enabled=False,
        )

        found = repository.find_by_binding(channel_kind="telegram", binding_key="chat-123")
        enabled = repository.list_enabled()

        self.assertIsNotNone(found)
        self.assertEqual(found.endpoint_id, "telegram_main")
        self.assertTrue(found.supports_typing)
        self.assertEqual(found.binding_metadata, {"chat_id": "123"})
        self.assertEqual([item.endpoint_id for item in enabled], ["telegram_main"])

    def test_channel_endpoint_repository_accepts_future_channel_kinds(self) -> None:
        repository = ChannelEndpointRepository()
        created = repository.upsert(
            endpoint_id="slack_main",
            channel_kind="slack",
            binding_key="workspace/channel/ops",
            enabled=True,
        )

        self.assertEqual(created.channel_kind, "slack")

    def test_llm_endpoint_repository_orders_enabled_endpoints_by_priority(self) -> None:
        repository = LLMEndpointRepository()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "anthropic_default",
                    "provider": "anthropic",
                    "model_id": "claude-sonnet",
                    "api_mode": "anthropic_messages",
                    "base_url": "https://api.anthropic.com/v1/messages",
                    "credential_ref": "anthropic-key",
                    "priority": 5,
                    "context_window": 200000,
                    "max_output_tokens": 8192,
                    "supports_reasoning": True,
                },
                {
                    "endpoint_id": "openai_default",
                    "provider": "openai",
                    "model_id": "gpt-5.4-mini",
                    "api_mode": "openai_chat",
                    "base_url": "https://api.openai.com/v1/chat/completions",
                    "credential_ref": "openai-key",
                    "priority": 1,
                    "context_window": 128000,
                    "max_output_tokens": 4096,
                    "supports_tools": True,
                    "supports_streaming": True,
                },
            ]
        )

        endpoints = repository.list_enabled()
        primary = repository.get_primary_enabled()

        self.assertEqual([item.endpoint_id for item in endpoints], ["openai_default", "anthropic_default"])
        self.assertIsNotNone(primary)
        self.assertEqual(primary.endpoint_id, "openai_default")

    def test_endpoint_resolver_loads_enabled_endpoints_once(self) -> None:
        repository = LLMEndpointRepository()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "first",
                    "provider": "stub",
                    "model_id": "first-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/first",
                    "credential_ref": "stub-first",
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        resolver = EndpointResolver(repository=repository)
        repository.upsert(
            endpoint_id="later",
            provider="stub",
            model_id="later-model",
            api_mode="openai_chat",
            base_url="stub://local/later",
            credential_ref="stub-later",
            priority=1,
            enabled=True,
        )

        self.assertEqual([item.endpoint_id for item in resolver.enabled()], ["first"])

    def test_llm_runtime_refreshes_endpoint_topology_from_database(self) -> None:
        repository = LLMEndpointRepository()
        settings = RuntimeSettingRepository()
        settings.ensure_defaults()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "old",
                    "provider": "stub",
                    "model_id": "old-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/old",
                    "credential_ref": "stub-old",
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        settings.set_active_llm_endpoint_id("old")
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings,
        )
        self.assertEqual([item.endpoint_id for item in runtime.endpoint_resolver.enabled()], ["old"])
        self.assertEqual(runtime.active_endpoint_id, "old")

        repository.upsert(
            endpoint_id="old",
            provider="stub",
            model_id="old-model",
            api_mode="openai_chat",
            base_url="stub://local/old",
            credential_ref="stub-old",
            priority=0,
            enabled=False,
        )
        repository.upsert(
            endpoint_id="new",
            provider="stub",
            model_id="new-model",
            api_mode="openai_chat",
            base_url="stub://local/new",
            credential_ref="stub-new",
            priority=0,
            enabled=True,
        )

        payload = runtime.refresh_llm_endpoints()

        self.assertEqual([item.endpoint_id for item in runtime.endpoint_resolver.enabled()], ["new"])
        self.assertEqual(payload["added_endpoint_ids"], ["new"])
        self.assertEqual(payload["removed_endpoint_ids"], ["old"])
        self.assertEqual(payload["configured_active_endpoint_id"], "old")
        self.assertIsNone(payload["active_endpoint_id"])
        self.assertEqual(payload["primary_endpoint_id"], "new")
        self.assertTrue(payload["credentials_refreshed"])
        self.assertTrue(payload["provider_adapters_refreshed"])

    def test_refresh_llm_endpoints_clears_cached_credentials(self) -> None:
        repository = LLMEndpointRepository()
        settings = RuntimeSettingRepository()
        endpoint = repository.upsert(
            endpoint_id="codex_bridge",
            provider="openai",
            model_id="openai/gpt-5.4",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="api_key_ref",
            credential_ref="codex_bridge:api-key",
            priority=0,
            enabled=True,
        )
        settings.set_active_llm_endpoint_id("codex_bridge")
        secret_store = InMemorySecretStore()
        secret_ref = SecretRef(service="codex_bridge", account="api-key")
        secret_store.set_secret(secret_ref, "old-token")
        resolver = LLMCredentialResolver(secret_store=secret_store)
        invoker = OpenAIChatEndpointInvoker(credentials=resolver)
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings,
            endpoint_invoker=invoker,
        )

        self.assertEqual(resolver.resolve_api_key(endpoint), "old-token")
        secret_store.set_secret(secret_ref, "new-token")
        self.assertEqual(resolver.resolve_api_key(endpoint), "old-token")

        payload = runtime.refresh_llm_endpoints()

        refreshed_endpoint = runtime.endpoint_resolver.primary(preferred_endpoint_id="codex_bridge")
        assert refreshed_endpoint is not None
        self.assertTrue(payload["credentials_refreshed"])
        self.assertTrue(payload["provider_adapters_refreshed"])
        self.assertEqual(resolver.resolve_api_key(refreshed_endpoint), "new-token")

    def test_resolve_max_output_tokens_refreshes_active_endpoint_setting(self) -> None:
        repository = LLMEndpointRepository()
        settings = RuntimeSettingRepository()
        settings.ensure_defaults()
        repository.upsert(
            endpoint_id="planner_long",
            provider="stub",
            model_id="planner-model",
            api_mode="openai_chat",
            base_url="stub://local/planner",
            credential_ref="stub-planner",
            priority=0,
            enabled=True,
            max_output_tokens=8192,
        )
        repository.upsert(
            endpoint_id="coder_fast",
            provider="stub",
            model_id="coder-model",
            api_mode="openai_chat",
            base_url="stub://local/coder",
            credential_ref="stub-coder",
            priority=1,
            enabled=True,
            max_output_tokens=4096,
        )
        settings.set_active_llm_endpoint_id("planner_long")
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings,
        )
        self.assertEqual(runtime.resolve_max_output_tokens(), 8192)

        settings.set_active_llm_endpoint_id("coder_fast")

        self.assertEqual(runtime.resolve_max_output_tokens(), 4096)
        self.assertEqual(runtime.resolve_max_output_tokens(preferred_endpoint_id="planner_long"), 8192)

    def test_openai_chat_credential_resolver_uses_secret_store_ref(self) -> None:
        repository = LLMEndpointRepository()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "deepseek",
                    "provider": "deepseek",
                    "model_id": "deepseek/deepseek-chat",
                    "api_mode": "openai_chat",
                    "base_url": "https://api.deepseek.com/chat/completions",
                    "credential_ref": "deepseek-prod",
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        endpoint = repository.get_primary_enabled()
        self.assertIsNotNone(endpoint)
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="deepseek-prod", account="api-key"), "sk-test")
        resolver = LLMCredentialResolver(secret_store=secret_store)

        secret = resolver.resolve_api_key(endpoint)

        assert endpoint is not None
        self.assertEqual(secret, "sk-test")
        self.assertEqual(resolver.secret_ref_for_endpoint(endpoint), SecretRef(service="deepseek-prod", account="api-key"))

    def test_openai_chat_credential_resolver_parses_service_account_ref(self) -> None:
        repository = LLMEndpointRepository()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "deepseek",
                    "provider": "deepseek",
                    "model_id": "deepseek/deepseek-chat",
                    "api_mode": "openai_chat",
                    "base_url": "https://api.deepseek.com/chat/completions",
                    "credential_ref": "deepseek-prod:api-key",
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        endpoint = repository.get_primary_enabled()
        self.assertIsNotNone(endpoint)
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="deepseek-prod", account="api-key"), "sk-test")
        resolver = LLMCredentialResolver(secret_store=secret_store)

        secret = resolver.resolve_api_key(endpoint)

        assert endpoint is not None
        self.assertEqual(secret, "sk-test")
        self.assertEqual(resolver.secret_ref_for_endpoint(endpoint), SecretRef(service="deepseek-prod", account="api-key"))

    def test_oauth_credential_resolver_uses_oauth_profile_ref(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="codex_oauth",
            provider="openai_codex_oauth",
            model_id="gpt-5.1-codex",
            api_mode="openai_chat",
            base_url="https://api.openai.com/v1/chat/completions",
            auth_kind="oauth",
            credential_ref="codex_oauth",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(
            SecretRef(service="codex_oauth", account="oauth-profile"),
            json.dumps(
                {
                    "access_token": "oauth-access-token",
                    "refresh_token": "oauth-refresh-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            ),
        )
        resolver = LLMCredentialResolver(secret_store=secret_store)

        auth = resolver.resolve_auth(endpoint)

        self.assertEqual(auth.kind, "oauth")
        self.assertEqual(auth.secret_ref, SecretRef(service="codex_oauth", account="oauth-profile"))
        self.assertEqual(auth.access_token, "oauth-access-token")
        self.assertEqual(auth.profile["refresh_token"], "oauth-refresh-token")
        self.assertEqual(resolver.resolve_api_key(endpoint), "oauth-access-token")

    def test_openai_chat_invoker_maps_oauth_access_token_to_bearer_key(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="codex_oauth",
            provider="openai_codex_oauth",
            model_id="gpt-5.1-codex",
            api_mode="openai_chat",
            base_url="https://api.openai.com/v1/chat/completions",
            auth_kind="oauth",
            credential_ref="codex_oauth:oauth-profile",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(
            SecretRef(service="codex_oauth", account="oauth-profile"),
            json.dumps({"access_token": "oauth-access-token"}),
        )
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
            ),
        )

        self.assertEqual(kwargs["api_key"], "oauth-access-token")
        self.assertEqual(kwargs["api_base"], "https://api.openai.com/v1")
        self.assertNotIn("oauth-access-token", str(invoker.last_payload_summary))

    def test_openai_chat_invoker_supplies_dummy_key_for_local_openai_endpoint(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="codex_bridge",
            provider="openai",
            model_id="gpt-5.4",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="local_provider_auth",
            credential_ref="",
            priority=0,
            enabled=True,
        )
        invoker = OpenAIChatEndpointInvoker()

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(messages=[{"role": "user", "content": "hello"}], max_output_tokens=64),
        )

        self.assertEqual(kwargs["api_key"], "local-provider-auth")
        self.assertEqual(kwargs["api_base"], "http://127.0.0.1:8765/v1")
        self.assertIn("input", kwargs)
        self.assertNotIn("messages", kwargs)

    def test_openai_chat_invoker_renders_openai_provider_as_responses_shape(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="openai-responses",
            provider="openai",
            model_id="gpt-5.4",
            api_mode="openai_chat",
            base_url="https://api.openai.com/v1",
            auth_kind="api_key_ref",
            credential_ref="openai-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="openai-prod", account="api-key"), "openai-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        shape, kwargs, _ = invoker._build_openai_request_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {"role": "system", "content": "system rules"},
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "probe", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "content": "probe result", "tool_call_id": "call_1"},
                ],
                max_output_tokens=64,
                metadata={"think_level": "deep"},
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "Probe.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            ),
        )

        self.assertEqual(shape, "responses")
        self.assertEqual(kwargs["api_key"], "openai-token")
        self.assertEqual(kwargs["model"], "gpt-5.4")
        self.assertNotIn("instructions", kwargs)
        self.assertEqual(kwargs["input"][0], {"role": "developer", "content": "system rules"})
        self.assertEqual(kwargs["reasoning"], {"effort": "high"})
        self.assertEqual(kwargs["tools"][0]["name"], "probe")
        self.assertNotIn("messages", kwargs)
        self.assertIn({"type": "function_call", "name": "probe", "arguments": "{}", "call_id": "call_1"}, kwargs["input"])
        self.assertIn({"type": "function_call_output", "call_id": "call_1", "output": "probe result"}, kwargs["input"])

    def test_openai_chat_invoker_wraps_chat_developer_instructions_as_user_context(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="deepseek",
            provider="deepseek",
            model_id="deepseek-v4-pro",
            api_mode="openai_chat",
            base_url="https://api.deepseek.com",
            auth_kind="api_key_ref",
            credential_ref="deepseek-prod:api-key",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="deepseek-prod", account="api-key"), "deepseek-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        shape, kwargs, _ = invoker._build_openai_request_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {"role": "system", "content": "initial rules"},
                    {"role": "user", "content": "hello"},
                    {"role": "developer", "content": "runtime guidance"},
                    {"role": "system", "content": "late system guidance"},
                ],
                max_output_tokens=64,
            ),
        )

        self.assertEqual(shape, "chat_completions")
        self.assertEqual(kwargs["messages"][0], {"role": "system", "content": "initial rules"})
        self.assertEqual(kwargs["messages"][2]["role"], "user")
        self.assertIn("<developer-instruction>", kwargs["messages"][2]["content"])
        self.assertIn("runtime guidance", kwargs["messages"][2]["content"])
        self.assertEqual(kwargs["messages"][3]["role"], "user")
        self.assertIn("<system-instruction>", kwargs["messages"][3]["content"])
        self.assertIn("late system guidance", kwargs["messages"][3]["content"])

    def test_openai_chat_invoker_forwards_generation_controls_for_codex_bridge_responses(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="codex_bridge",
            provider="codex_bridge",
            model_id="hosted_vllm/gpt-5.4",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="api_key_ref",
            credential_ref="codex_bridge:api-key",
            priority=0,
            enabled=True,
            capabilities_blob={"codex_bridge": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="codex_bridge", account="api-key"), "bridge-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        shape, kwargs, _ = invoker._build_openai_request_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                temperature=0.7,
                metadata={"think_level": "xhigh"},
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "Probe.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            ),
        )

        self.assertEqual(shape, "responses")
        self.assertEqual(kwargs["api_key"], "bridge-token")
        self.assertEqual(kwargs["model"], "gpt-5.4")
        self.assertIn("input", kwargs)
        self.assertNotIn("messages", kwargs)
        self.assertIn("tools", kwargs)
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertEqual(kwargs["reasoning"], {"effort": "xhigh"})
        self.assertNotIn("extra_body", kwargs)

    def test_routing_invoker_uses_native_openai_responses_sdk(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="openai-native",
            provider="openai",
            model_id="gpt-5.4",
            api_mode="openai_chat",
            base_url="https://api.openai.com/v1",
            auth_kind="api_key_ref",
            credential_ref="openai-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="openai-prod", account="api-key"), "openai-token")
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeResponse:
            def to_dict(self):
                return {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "pong"}],
                        }
                    ]
                }

        class FakeResponses:
            def create(self, **kwargs):
                calls.append(("create", dict(kwargs)))
                return FakeResponse()

        class FakeOpenAIClient:
            def __init__(self, **kwargs):
                calls.append(("client", dict(kwargs)))
                self.responses = FakeResponses()

        fake_openai = types.SimpleNamespace(OpenAI=FakeOpenAIClient)
        invoker = build_default_endpoint_invoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            outcome = invoker.invoke(
                endpoint,
                CanonicalLLMRequest(
                    messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}],
                    max_output_tokens=64,
                    metadata={"think_level": "deep"},
                ),
            )

        self.assertEqual(outcome.text, "pong")
        self.assertEqual(calls[0][0], "client")
        self.assertEqual(calls[0][1]["api_key"], "openai-token")
        self.assertEqual(calls[0][1]["base_url"], "https://api.openai.com/v1")
        self.assertEqual(calls[0][1]["max_retries"], 0)
        self.assertEqual(calls[1][0], "create")
        self.assertEqual(calls[1][1]["model"], "gpt-5.4")
        self.assertNotIn("instructions", calls[1][1])
        self.assertEqual(calls[1][1]["input"][0], {"role": "developer", "content": "rules"})
        self.assertEqual(calls[1][1]["reasoning"], {"effort": "high"})
        self.assertIn("input", calls[1][1])
        self.assertNotIn("messages", calls[1][1])

    def test_routing_invoker_uses_native_anthropic_messages_sdk(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="anthropic-native",
            provider="anthropic",
            model_id="anthropic/claude-sonnet-4-5",
            api_mode="anthropic_messages",
            base_url="https://api.anthropic.com/v1/messages",
            auth_kind="api_key_ref",
            credential_ref="anthropic-prod:api-key",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="anthropic-prod", account="api-key"), "anthropic-token")
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeResponse:
            def to_dict(self):
                return {
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "text", "text": "thinking done"},
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "probe_alias",
                            "input": {"ok": True},
                        },
                    ],
                }

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(("create", dict(kwargs)))
                return FakeResponse()

        class FakeAnthropicClient:
            def __init__(self, **kwargs):
                calls.append(("client", dict(kwargs)))
                self.messages = FakeMessages()

        fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)
        invoker = build_default_endpoint_invoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            outcome = invoker.invoke(
                endpoint,
                CanonicalLLMRequest(
                    messages=[
                        {"role": "system", "content": "rules"},
                        {"role": "user", "content": "hello"},
                        {"role": "developer", "content": "runtime guidance"},
                        {"role": "system", "content": "late system guidance"},
                    ],
                    max_output_tokens=4096,
                    metadata={"think_level": "deep"},
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "probe_alias",
                                "description": "Probe.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                ),
            )

        self.assertEqual(outcome.text, "thinking done")
        self.assertEqual(outcome.finish_reason, LLMFinishReason.TOOL_CALLS)
        self.assertEqual(outcome.tool_calls[0].name, "probe_alias")
        self.assertEqual(outcome.tool_calls[0].args, {"ok": True})
        self.assertEqual(outcome.tool_calls[0].call_id, "call_1")
        self.assertEqual(calls[0][0], "client")
        self.assertEqual(calls[0][1]["api_key"], "anthropic-token")
        self.assertEqual(calls[0][1]["base_url"], "https://api.anthropic.com")
        self.assertEqual(calls[0][1]["max_retries"], 0)
        self.assertEqual(calls[1][0], "create")
        self.assertEqual(calls[1][1]["model"], "claude-sonnet-4-5")
        self.assertEqual(calls[1][1]["system"], "rules")
        rendered_messages = calls[1][1]["messages"]
        self.assertEqual(len(rendered_messages), 1)
        self.assertEqual(rendered_messages[0]["role"], "user")
        rendered_content = rendered_messages[0]["content"]
        self.assertEqual(rendered_content[0]["text"], "hello")
        self.assertIn("<developer-instruction>", rendered_content[1]["text"])
        self.assertIn("runtime guidance", rendered_content[1]["text"])
        self.assertIn("<system-instruction>", rendered_content[2]["text"])
        self.assertIn("late system guidance", rendered_content[2]["text"])
        self.assertEqual(calls[1][1]["tools"][0]["name"], "probe_alias")
        self.assertEqual(calls[1][1]["thinking"], {"type": "enabled", "budget_tokens": 2048})

    def test_openai_chat_invoker_maps_glm_think_level_to_thinking_body(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5.2",
            provider="zhipu",
            model_id="glm-5.2",
            api_mode="openai_chat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
            capabilities_blob={"supports_thinking": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                metadata={"think_level": "xhigh"},
            ),
        )

        self.assertEqual(kwargs["model"], "glm-5.2")
        self.assertEqual(kwargs["thinking"], {"type": "enabled"})
        self.assertEqual(kwargs["reasoning_effort"], "max")
        self.assertNotIn("extra_body", kwargs)
        self.assertNotIn("behavior-routing-reminder", str(kwargs["messages"]))
        _, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(request_kwargs["reasoning_effort"], "max")
        self.assertNotIn("thinking", request_kwargs)

    def test_openai_chat_invoker_replays_reasoning_content_for_openai_shape_reasoning_providers(self) -> None:
        cases = [
            ("generic-reasoner", "openai_compatible", "generic-reasoner", "https://example.test/v1", "generic-reasoner"),
            ("glm-5", "zhipu", "glm-5.1", "https://api.z.ai/api/coding/paas/v4", "glm-5.1"),
            ("deepseek-r1", "deepseek", "deepseek-reasoner", "https://api.deepseek.com", "deepseek-reasoner"),
        ]
        for endpoint_id, provider, model_id, base_url, expected_model in cases:
            with self.subTest(endpoint_id=endpoint_id):
                endpoint = LLMEndpointRepository().upsert(
                    endpoint_id=endpoint_id,
                    provider=provider,
                    model_id=model_id,
                    api_mode="openai_chat",
                    base_url=base_url,
                    auth_kind="api_key_ref",
                    credential_ref=f"{endpoint_id}:api-key",
                    priority=0,
                    enabled=True,
                    supports_reasoning=True,
                    capabilities_blob={"supports_thinking": True},
                )
                secret_store = InMemorySecretStore()
                secret_store.set_secret(SecretRef(service=endpoint_id, account="api-key"), "token")
                invoker = OpenAIChatEndpointInvoker(
                    credentials=LLMCredentialResolver(secret_store=secret_store)
                )

                kwargs, _ = invoker._build_completion_kwargs(
                    endpoint,
                    CanonicalLLMRequest(
                        messages=[
                            {"role": "user", "content": "run probe"},
                            {
                                "role": "assistant",
                                "content": "",
                                "provider_specific_fields": {"reasoning_content": "hidden reasoning"},
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "probe_tool", "arguments": "{}"},
                                    }
                                ],
                            },
                            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
                        ],
                        max_output_tokens=64,
                        metadata={"think_level": "high"},
                    ),
                )

                assistant_message = next(message for message in kwargs["messages"] if message["role"] == "assistant")
                self.assertEqual(kwargs["model"], expected_model)
                self.assertEqual(assistant_message["reasoning_content"], "hidden reasoning")
                self.assertNotIn("provider_specific_fields", assistant_message)

    def test_openai_compatible_message_renderer_strips_internal_provider_fields_by_default(self) -> None:
        rendered = chat_messages_to_openai_compatible_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "hidden top-level",
                    "provider_specific_fields": {"reasoning_content": "hidden nested"},
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "probe_tool", "arguments": "{}"},
                        }
                    ],
                }
            ]
        )

        self.assertEqual(rendered[0]["role"], "assistant")
        self.assertNotIn("reasoning_content", rendered[0])
        self.assertNotIn("provider_specific_fields", rendered[0])

    def test_openai_chat_invoker_appends_zai_tool_routing_reminder_to_last_user_message(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-tools",
            provider="zhipu",
            model_id="glm-5.1",
            api_mode="openai_chat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
            capabilities_blob={"supports_thinking": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store),
            message_hooks=MAIN_LLM_REQUEST_HOOKS,
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "Implement the task."},
                    {"role": "assistant", "content": "I will work on it."},
                    {"role": "user", "content": "Continue."},
                ],
                max_output_tokens=64,
                tools=[
                    {"type": "function", "function": {"name": "run_shell", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "delete_path", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "tree", "parameters": {"type": "object"}}},
                ],
                metadata={"think_level": "high"},
            ),
        )

        rendered_messages = kwargs["messages"]
        self.assertEqual(rendered_messages[-1]["role"], "user")
        self.assertTrue(str(rendered_messages[-1]["content"]).startswith("Continue."))
        self.assertIn("<system-reminder id=\"behavior-routing-reminder\">", rendered_messages[-1]["content"])
        self.assertIn("Behavior-routing guidance", rendered_messages[-1]["content"])
        self.assertIn("Choose the smallest available capability", rendered_messages[-1]["content"])
        self.assertIn("Do not guess unavailable capability names", rendered_messages[-1]["content"])
        self.assertNotIn("op_file_write", rendered_messages[-1]["content"])
        self.assertNotIn("op_path_delete", rendered_messages[-1]["content"])
        self.assertNotIn("op_exec_shell", rendered_messages[-1]["content"])
        self.assertNotIn("behavior-routing-reminder", str(rendered_messages[0:3]))
        self.assertNotIn("tool-routing hook", rendered_messages[-1]["content"])
        self.assertNotIn("surface=", rendered_messages[-1]["content"])
        self.assertEqual(kwargs["thinking"], {"type": "enabled"})
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertNotIn("extra_body", kwargs)
        _, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "enabled"}})

    def test_openai_chat_invoker_does_not_append_tool_routing_reminder_without_registered_hooks(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-tools-no-hooks",
            provider="zhipu",
            model_id="glm-5.1",
            api_mode="openai_chat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "Continue."}],
                max_output_tokens=64,
                tools=[
                    {"type": "function", "function": {"name": "run_shell", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
                ],
            ),
        )

        self.assertNotIn("behavior-routing-reminder", str(kwargs["messages"]))

    def test_openai_chat_invoker_accepts_legacy_main_behavior_routing_hook_name(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-tools-legacy-hook",
            provider="zhipu",
            model_id="glm-5.1",
            api_mode="openai_chat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store),
            message_hooks=("main_behavior_routing",),
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "Implement the task."}],
                max_output_tokens=64,
                tools=[
                    {"type": "function", "function": {"name": "run_shell", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}},
                ],
            ),
        )

        self.assertIn("<system-reminder id=\"behavior-routing-reminder\">", kwargs["messages"][-1]["content"])

    def test_openai_chat_invoker_does_not_append_zai_tool_routing_reminder_without_shell_tool(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-file-only",
            provider="zhipu",
            model_id="glm-5.1",
            api_mode="openai_chat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                tools=[
                    {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
                ],
            ),
        )

        self.assertNotIn("behavior-routing-reminder", str(kwargs["messages"]))

    def test_openai_chat_zai_tool_routing_reminder_does_not_name_unavailable_repo_tools(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-main-tools",
            provider="zhipu",
            model_id="glm-5.1",
            api_mode="openai_chat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store),
            message_hooks=MAIN_LLM_REQUEST_HOOKS,
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {
                        "role": "user",
                        "content": "The previous log mentioned op_tree and op_search, but this main runtime only has file tools.",
                    }
                ],
                max_output_tokens=64,
                tools=[
                    {"type": "function", "function": {"name": "run_shell", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
                ],
            ),
        )

        rendered = str(kwargs["messages"][-1]["content"])
        self.assertIn("behavior-routing-reminder", rendered)
        reminder = rendered.split("<system-reminder", 1)[1]
        self.assertNotIn("op_file_write", reminder)
        self.assertNotIn("op_tree", reminder)
        self.assertNotIn("op_search", reminder)
        self.assertIn("Do not guess unavailable capability names", reminder)
        self.assertNotIn("tool-routing hook", reminder)
        self.assertNotIn("surface=", reminder)

    def test_anthropic_invoker_appends_zai_tool_routing_reminder_for_zhipu_anthropic_shape(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-anthropic-shape",
            provider="zhipu",
            model_id="glm-5.2",
            api_mode="anthropic_messages",
            base_url="https://open.bigmodel.cn/api/anthropic",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
            capabilities_blob={"supports_thinking": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = ZaiAnthropicMessagesEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store),
            message_hooks=MAIN_LLM_REQUEST_HOOKS,
        )

        client_kwargs, request_kwargs, _ = invoker._build_messages_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "Continue."}],
                max_output_tokens=4096,
                tools=[
                    {"type": "function", "function": {"name": "run_shell", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "delete_path", "parameters": {"type": "object"}}},
                ],
                metadata={"think_level": "high"},
            ),
        )

        self.assertEqual(client_kwargs["api_key"], "glm-token")
        self.assertEqual(client_kwargs["base_url"], "https://open.bigmodel.cn/api/anthropic")
        self.assertEqual(request_kwargs["thinking"], {"type": "enabled", "budget_tokens": 2048})
        rendered_messages = request_kwargs["messages"]
        self.assertEqual(rendered_messages[-1]["role"], "user")
        final_text = rendered_messages[-1]["content"][-1]["text"]
        self.assertIn("<system-reminder id=\"behavior-routing-reminder\">", final_text)
        self.assertIn("Behavior-routing guidance", final_text)
        self.assertIn("Choose the smallest available capability", final_text)
        self.assertNotIn("op_exec_shell", final_text)
        self.assertNotIn("op_file_write", final_text)
        self.assertNotIn("op_path_delete", final_text)
        self.assertNotIn("tool-routing hook", final_text)
        self.assertNotIn("surface=", final_text)

    def test_routing_invoker_prefers_zai_anthropic_shell_over_generic_anthropic(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-anthropic-route",
            provider="zhipu",
            model_id="glm-5.2",
            api_mode="anthropic_messages",
            base_url="https://open.bigmodel.cn/api/anthropic",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
        )
        invoker = build_default_endpoint_invoker()

        selected = invoker._select(endpoint)

        self.assertIsInstance(selected, ZaiAnthropicMessagesEndpointInvoker)

    def test_openai_chat_invoker_maps_glm_off_to_disabled_thinking_body(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="glm-5-off",
            provider="zhipu",
            model_id="glm-5.1",
            api_mode="openai_chat",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            auth_kind="api_key_ref",
            credential_ref="glm-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
            capabilities_blob={"supports_thinking": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="glm-prod", account="api-key"), "glm-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                metadata={"think_level": "off"},
            ),
        )

        self.assertEqual(kwargs["model"], "glm-5.1")
        self.assertEqual(kwargs["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("extra_body", kwargs)
        _, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_openai_chat_invoker_maps_deepseek_think_level_to_reasoning_and_thinking_body(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="deepseek-v4-pro",
            provider="deepseek",
            model_id="deepseek-v4-pro",
            api_mode="openai_chat",
            base_url="https://api.deepseek.com",
            auth_kind="api_key_ref",
            credential_ref="deepseek-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
            capabilities_blob={"supports_thinking": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="deepseek-prod", account="api-key"), "deepseek-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                metadata={"think_level": "xhigh"},
            ),
        )

        self.assertEqual(kwargs["api_key"], "deepseek-token")
        self.assertEqual(kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual(kwargs["thinking"], {"type": "enabled"})
        self.assertNotIn("extra_body", kwargs)
        _, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "enabled"}})

    def test_openai_chat_invoker_maps_deepseek_off_to_disabled_thinking_without_reasoning_effort(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="deepseek-v4-pro-off",
            provider="deepseek",
            model_id="deepseek-v4-pro",
            api_mode="openai_chat",
            base_url="https://api.deepseek.com",
            auth_kind="api_key_ref",
            credential_ref="deepseek-prod:api-key",
            priority=0,
            enabled=True,
            supports_reasoning=True,
            capabilities_blob={"supports_thinking": True},
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(SecretRef(service="deepseek-prod", account="api-key"), "deepseek-token")
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=secret_store)
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                metadata={"think_level": "off"},
            ),
        )

        self.assertEqual(kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(kwargs["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("extra_body", kwargs)
        _, request_kwargs = _split_openai_chat_sdk_kwargs(kwargs)
        self.assertEqual(request_kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_llm_provider_registry_can_register_runtime_provider(self) -> None:
        class DemoProvider(LLMProviderAdapter):
            provider_names = frozenset({"demo_provider"})
            model_provider_prefix = "hosted_vllm"

            def apply_request(self, request: CanonicalLLMRequest, draft) -> None:  # type: ignore[no-untyped-def]
                draft.extra["seed"] = 7

        registry = LLMProviderRegistry()
        registry.register(DemoProvider)
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="demo",
            provider="demo_provider",
            model_id="demo-model",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="local_provider_auth",
            credential_ref="",
            priority=0,
            enabled=True,
        )

        adapter = registry.resolve(endpoint)
        draft = adapter.new_draft([{"role": "user", "content": "hello"}])
        adapter.apply_request(CanonicalLLMRequest(messages=[], max_output_tokens=16), draft)
        kwargs = draft.to_kwargs()

        self.assertEqual(kwargs["model"], "demo-model")
        self.assertEqual(kwargs["seed"], 7)

    def test_llm_provider_registry_can_unregister_runtime_provider(self) -> None:
        class DemoProvider(LLMProviderAdapter):
            provider_names = frozenset({"demo_unregister"})
            model_provider_prefix = "hosted_vllm"

        registry = LLMProviderRegistry()
        registry.register(DemoProvider)
        registry.unregister(DemoProvider)
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="demo_unregister",
            provider="demo_unregister",
            model_id="demo-model",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="local_provider_auth",
            credential_ref="",
            priority=0,
            enabled=True,
        )

        adapter = registry.resolve(endpoint)

        self.assertEqual(adapter.api_model(), "demo-model")

    def test_llm_provider_registry_restores_builtin_mapping_after_runtime_adapter_removed(self) -> None:
        adapters_dir = self.runtime_root / "llm" / "adapters"
        adapters_dir.mkdir(parents=True, exist_ok=True)
        adapter_path = adapters_dir / "override_openai.py"
        adapter_path.write_text(
            "\n".join(
                [
                    "from pal.llm import LLMProviderAdapter",
                    "",
                    "class RuntimeOpenAIOverride(LLMProviderAdapter):",
                    "    provider_names = frozenset({'openai'})",
                    "    model_provider_prefix = 'hosted_vllm'",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        registry = build_default_provider_registry(load_entry_points=False)
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="openai_restore",
            provider="openai",
            model_id="demo-model",
            api_mode="openai_chat",
            base_url="https://api.openai.com/v1",
            auth_kind="api_key_ref",
            credential_ref="",
            priority=0,
            enabled=True,
        )

        registry.load_runtime_adapters(self.runtime_root)
        self.assertEqual(registry.resolve(endpoint).api_model(), "demo-model")

        adapter_path.unlink()
        registry.load_runtime_adapters(self.runtime_root)

        self.assertEqual(registry.resolve(endpoint).api_model(), "demo-model")

    def test_openai_chat_invoker_uses_injected_provider_registry(self) -> None:
        class DemoProvider(LLMProviderAdapter):
            provider_names = frozenset({"demo_injected"})
            model_provider_prefix = "hosted_vllm"

            def apply_request(self, request: CanonicalLLMRequest, draft) -> None:  # type: ignore[no-untyped-def]
                _ = request
                draft.extra["seed"] = 11

        registry = build_default_provider_registry(load_entry_points=False)
        registry.register(DemoProvider)
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="demo_injected",
            provider="demo_injected",
            model_id="demo-model",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="local_provider_auth",
            credential_ref="",
            priority=0,
            enabled=True,
        )
        invoker = OpenAIChatEndpointInvoker(provider_registry=registry)

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(messages=[{"role": "user", "content": "hello"}], max_output_tokens=16),
        )

        self.assertEqual(kwargs["model"], "demo-model")
        self.assertEqual(kwargs["seed"], 11)

    def test_openai_chat_invoker_sets_openai_chat_and_sdk_timeouts(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="timeout_demo",
            provider="openai_compatible",
            model_id="demo-model",
            api_mode="openai_chat",
            base_url="https://example.invalid/v1",
            auth_kind="local_provider_auth",
            credential_ref="",
            priority=0,
            enabled=True,
        )
        invoker = OpenAIChatEndpointInvoker()

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=16,
                metadata={"timeout_seconds": 37.0},
            ),
        )

        self.assertEqual(kwargs["timeout"], 37)
        self.assertEqual(kwargs["request_timeout"], 37.0)
        self.assertEqual(kwargs["force_timeout"], 37.0)
        self.assertEqual(kwargs["max_retries"], 0)

    def test_routing_invoker_exposes_selected_provider_payload_summary(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="stub_route",
            provider="stub",
            model_id="stub-model",
            api_mode="openai_chat",
            base_url="stub://local/route",
            credential_ref="",
            priority=0,
            enabled=True,
        )
        invoker = build_default_endpoint_invoker()

        outcome = invoker.invoke(
            endpoint,
            CanonicalLLMRequest(messages=[{"role": "user", "content": "hello"}], max_output_tokens=16),
        )

        self.assertEqual(outcome.text, "hello")
        self.assertEqual(invoker.last_payload_summary["endpoint_id"], "stub_route")
        self.assertEqual(invoker.last_payload_summary["message_count"], 1)

    def test_llm_runtime_logs_typed_failure_without_persisting_request_content(self) -> None:
        class EmptyResponseInvoker:
            def __init__(self) -> None:
                self.last_payload_summary: dict[str, object] = {}

            def invoke(self, endpoint, request):
                _ = endpoint
                self.last_payload_summary = {
                    "message_count": len(request.messages),
                    "roles": [message.get("role") for message in request.messages],
                    "authorization": "Bearer secret-provider-token",
                    "image_parts": [
                        {
                            "message_index": 1,
                            "part_index": 0,
                            "transport": "http_url",
                            "prefix": "https://private.invalid/user-secret-image",
                            "url_length": 49,
                        }
                    ],
                }
                raise LLMEndpointInvocationError(
                    "llm response contained no assistant content or tool calls; echoed first user message"
                )

            def invoke_stream(self, endpoint, request):
                _ = endpoint, request
                raise NotImplementedError

        endpoint_repository = LLMEndpointRepository()
        endpoint_repository.upsert(
            endpoint_id="glm_demo",
            provider="openai_compatible",
            model_id="glm-demo",
            api_mode="openai_chat",
            base_url="https://example.invalid/v1",
            credential_ref="secret-provider-token",
            priority=0,
            enabled=True,
        )
        events: list[dict[str, object]] = []
        legacy_audit_dir = self.runtime_root / "data" / "llm" / "audit"
        legacy_audit_dir.mkdir(parents=True, exist_ok=True)
        legacy_audit = legacy_audit_dir / "llm_failure_legacy.json"
        legacy_audit.write_text('{"messages":["legacy private prompt"]}', encoding="utf-8")
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=RuntimeSettingRepository(),
            endpoint_invoker=EmptyResponseInvoker(),
            config=RuntimeConfig(runtime_root=self.runtime_root, llm_endpoint_retry_attempts=1),
            event_sink=events.append,
        )
        self.assertFalse(legacy_audit.exists())

        with self.assertLogs("pal.llm.runtime", level="WARNING") as captured:
            outcome = runtime.generate(
                CanonicalLLMRequest(
                    messages=[
                        {"role": "system", "content": "stable instructions"},
                        {"role": "user", "content": "first user message"},
                        {"role": "user", "content": "second user message"},
                    ],
                    max_output_tokens=64,
                    tools=[{"type": "function", "function": {"name": "demo", "parameters": {"type": "object"}}}],
                    metadata={
                        "purpose": "minion_worker",
                        "api_key": "secret-api-key",
                        "prompt_observation_tag": "obs_failure_audit",
                    },
                )
            )

        self.assertEqual(outcome.finish_reason, LLMFinishReason.ERROR)
        audit_files = sorted((self.runtime_root / "data" / "llm" / "audit").glob("llm_failure_*.json"))
        self.assertEqual(audit_files, [])
        self.assertNotIn("error_message", events[0])
        self.assertEqual(events[0]["error_type"], "LLMEndpointInvocationError")
        logs = "\n".join(captured.output)
        self.assertIn("error_kind=unknown", logs)
        self.assertIn("error_type=LLMEndpointInvocationError", logs)
        for secret in (
            "stable instructions",
            "first user message",
            "second user message",
            "parameters",
            "obs_failure_audit",
            "private.invalid",
            "secret-api-key",
            "secret-provider-token",
        ):
            self.assertNotIn(secret, logs)

    def test_openai_chat_wall_timeout_returns_without_waiting_for_stuck_call(self) -> None:
        from pal.llm.runtime import LLMEndpointInvocationError, _run_llm_with_wall_timeout

        started_at = time.monotonic()
        with self.assertRaises(LLMEndpointInvocationError):
            _run_llm_with_wall_timeout(
                lambda: time.sleep(2.0),
                timeout_seconds=0.05,
                description="test openai_chat call",
            )

        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_refresh_llm_endpoints_reloads_runtime_root_provider_adapters(self) -> None:
        adapters_dir = self.runtime_root / "llm" / "adapters"
        adapters_dir.mkdir(parents=True, exist_ok=True)
        adapter_path = adapters_dir / "runtime_demo.py"

        def write_adapter(seed: int) -> None:
            adapter_path.write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "from pal.llm import CanonicalLLMRequest, LLMProviderAdapter, OpenAIChatCompletionDraft",
                        "",
                        "class RuntimeDemoProvider(LLMProviderAdapter):",
                        "    provider_names = frozenset({'runtime_demo'})",
                        "    model_provider_prefix = 'hosted_vllm'",
                        "",
                        "    def apply_request(self, request: CanonicalLLMRequest, draft: OpenAIChatCompletionDraft) -> None:",
                        "        _ = request",
                        f"        draft.extra['seed'] = {seed}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

        write_adapter(1)
        repository = LLMEndpointRepository()
        settings = RuntimeSettingRepository()
        endpoint = repository.upsert(
            endpoint_id="runtime_demo",
            provider="runtime_demo",
            model_id="demo-model",
            api_mode="openai_chat",
            base_url="http://127.0.0.1:8765/v1",
            auth_kind="local_provider_auth",
            credential_ref="",
            priority=0,
            enabled=True,
        )
        invoker = OpenAIChatEndpointInvoker(runtime_root=self.runtime_root)
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings,
            endpoint_invoker=invoker,
        )
        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(messages=[{"role": "user", "content": "hello"}], max_output_tokens=16),
        )
        self.assertEqual(kwargs["seed"], 1)

        write_adapter(2)
        payload = runtime.refresh_llm_endpoints()
        refreshed_endpoint = runtime.endpoint_resolver.primary()
        assert refreshed_endpoint is not None
        kwargs, _ = invoker._build_completion_kwargs(
            refreshed_endpoint,
            CanonicalLLMRequest(messages=[{"role": "user", "content": "hello"}], max_output_tokens=16),
        )

        self.assertTrue(payload["provider_adapters_refreshed"])
        self.assertEqual(payload["provider_adapter_load_errors"], [])
        self.assertEqual(kwargs["model"], "demo-model")
        self.assertEqual(kwargs["seed"], 2)

    def test_encrypted_file_secret_store_reloads_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_secret_reload_test_") as tmp:
            secrets_path = Path(tmp) / "secrets.json"
            first = EncryptedFileSecretStore(secrets_path)
            ref = SecretRef(service="codex_bridge", account="api-key")
            self.assertIsNone(first.get_secret(ref))

            second = EncryptedFileSecretStore(secrets_path)
            second.set_secret(ref, "bridge-token")

            self.assertEqual(first.get_secret(ref), "bridge-token")

    def test_encrypted_file_secret_store_survives_hostname_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_secret_hostname_test_") as tmp:
            secrets_path = Path(tmp) / "secrets.json"
            ref = SecretRef(service="codex_bridge", account="api-key")

            with patch("pal.llm.secret_store.socket.gethostname", return_value="before-reboot"):
                first = EncryptedFileSecretStore(secrets_path)
                first.set_secret(ref, "bridge-token")

            with patch("pal.llm.secret_store.socket.gethostname", return_value="after-reboot"):
                second = EncryptedFileSecretStore(secrets_path)

            self.assertEqual(second.get_secret(ref), "bridge-token")

    def test_encrypted_file_secret_store_migrates_legacy_hostname_key(self) -> None:
        from cryptography.fernet import Fernet
        from pal.llm.secret_store import _derive_fernet_key

        with tempfile.TemporaryDirectory(prefix="pal_secret_legacy_hostname_test_") as tmp:
            secrets_path = Path(tmp) / "secrets.json"
            ref = SecretRef(service="codex_bridge", account="api-key")
            with patch("pal.llm.secret_store.socket.gethostname", return_value="current-host"):
                old_key = _derive_fernet_key(runtime_root=str(secrets_path.parent), include_hostname=True)
            encrypted = Fernet(old_key).encrypt(b"bridge-token").decode()
            secrets_path.write_text(
                json.dumps(
                    {
                        "codex_bridge:api-key": {
                            "service": "codex_bridge",
                            "account": "api-key",
                            "encrypted": encrypted,
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch("pal.llm.secret_store.socket.gethostname", return_value="current-host"):
                migrated = EncryptedFileSecretStore(secrets_path)
                self.assertEqual(migrated.get_secret(ref), "bridge-token")

            with patch("pal.llm.secret_store.socket.gethostname", return_value="changed-host"):
                stable = EncryptedFileSecretStore(secrets_path)

            self.assertEqual(stable.get_secret(ref), "bridge-token")

    def test_oauth_profile_without_access_token_does_not_become_api_key(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="codex_oauth",
            provider="openai_codex_oauth",
            model_id="gpt-5.1-codex",
            api_mode="openai_chat",
            base_url="https://api.openai.com/v1/chat/completions",
            auth_kind="oauth",
            credential_ref="codex_oauth",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(
            SecretRef(service="codex_oauth", account="oauth-profile"),
            json.dumps({"refresh_token": "oauth-refresh-token"}),
        )
        resolver = LLMCredentialResolver(secret_store=secret_store)

        auth = resolver.resolve_auth(endpoint)

        self.assertIsNone(auth.access_token)
        self.assertIsNone(resolver.resolve_api_key(endpoint))

    def test_llm_runtime_preflight_reads_budget_from_database(self) -> None:
        repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        settings_repository.ensure_defaults()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "stub_endpoint",
                    "provider": "stub",
                    "model_id": "stub-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/llm",
                    "credential_ref": "stub-key",
                    "context_window": 10000,
                    "max_output_tokens": 1200,
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings_repository,
        )

        advice = runtime.preflight(
            LLMPreflightRequest(
                messages=[{"role": "system", "content": "x" * 5000}],
                max_output_tokens=1500,
            )
        )

        self.assertEqual(advice.active_model, "stub-model")
        self.assertEqual(advice.reserved_output_tokens, 1200)
        self.assertEqual(advice.target_input_budget, 11108)
        self.assertEqual(advice.fallback_chain, [])
        self.assertEqual(advice.breakdown["system_chars"], 5000)
        self.assertEqual(advice.breakdown["tool_protocol_chars"], 0)
        self.assertEqual(advice.breakdown["tools_schema_chars"], 0)
        self.assertEqual(advice.breakdown["conversation_chars"], 0)
        self.assertEqual(advice.breakdown["current_user_chars"], 0)
        self.assertEqual(advice.breakdown["hard_keep_chars"], 5000)
        self.assertEqual(advice.breakdown["estimated_input_chars"], 5000)
        self.assertEqual(advice.breakdown["available_input_budget_chars"], 27216)
        self.assertFalse(bool(advice.breakdown["hard_overflow"]))

    def test_llm_runtime_preflight_marks_hard_overflow_when_hard_keep_exceeds_budget(self) -> None:
        repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        settings_repository.ensure_defaults()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "tiny_endpoint",
                    "provider": "stub",
                    "model_id": "tiny-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/llm",
                    "credential_ref": "stub-key",
                    "context_window": 2000,
                    "max_output_tokens": 256,
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings_repository,
            config=RuntimeConfig(),
        )

        advice = runtime.preflight(
            LLMPreflightRequest(
                messages=[
                    {"role": "system", "content": "s" * 3000},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
                    {"role": "tool", "content": "t" * 2000, "tool_call_id": "call_1"},
                    {"role": "user", "content": "u" * 1000},
                ],
                max_output_tokens=512,
            )
        )

        self.assertEqual(advice.status, "compact_required")
        self.assertTrue(bool(advice.breakdown["hard_overflow"]))
        self.assertEqual(advice.breakdown["system_chars"], 3000)
        self.assertGreaterEqual(advice.breakdown["tool_protocol_chars"], 2000)
        self.assertEqual(advice.breakdown["tools_schema_chars"], 0)
        self.assertEqual(advice.breakdown["current_user_chars"], 1000)
        self.assertGreaterEqual(advice.breakdown["hard_keep_chars"], 6000)
        self.assertLess(advice.breakdown["available_input_budget_chars"], advice.breakdown["hard_keep_chars"])

    def test_llm_runtime_preflight_counts_tool_schema_budget(self) -> None:
        repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        settings_repository.ensure_defaults()
        repository.ensure_defaults(
            [
                {
                    "endpoint_id": "stub_endpoint",
                    "provider": "stub",
                    "model_id": "stub-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/llm",
                    "credential_ref": "stub-key",
                    "context_window": 10000,
                    "max_output_tokens": 1200,
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=repository),
            settings_repository=settings_repository,
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "d" * 500,
                    "parameters": {
                        "type": "object",
                        "properties": {"payload": {"type": "string", "description": "x" * 300}},
                    },
                },
            }
        ]
        expected_tool_chars = len(json.dumps(tools, ensure_ascii=False, sort_keys=True))

        advice = runtime.preflight(
            LLMPreflightRequest(
                messages=[{"role": "user", "content": "u"}],
                max_output_tokens=256,
                tools=tools,
            )
        )

        self.assertEqual(advice.breakdown["tools_schema_chars"], expected_tool_chars)
        self.assertEqual(advice.breakdown["estimated_input_chars"], expected_tool_chars + 1)
        self.assertEqual(advice.breakdown["hard_keep_chars"], expected_tool_chars + 1)

    def test_runtime_setting_repository_persists_think_level(self) -> None:
        repository = RuntimeSettingRepository()

        repository.ensure_defaults()
        self.assertIsNone(repository.get_think_level("endpoint-a"))

        repository.set_think_level("endpoint-a", "deep")
        repository.set_think_level("endpoint-b", "low")
        self.assertEqual(repository.get_think_level("endpoint-a"), "deep")
        self.assertEqual(repository.get_think_level("endpoint-b"), "low")

    def test_llm_runtime_uses_provider_choices_and_persists_them_per_endpoint(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "glm",
                    "provider": "zhipu",
                    "model_id": "glm-5.2",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/glm",
                    "credential_ref": "stub-key",
                    "supports_reasoning": True,
                    "capabilities_blob": {"supports_thinking": True},
                    "priority": 0,
                    "enabled": True,
                },
                {
                    "endpoint_id": "claude",
                    "provider": "anthropic",
                    "model_id": "claude-sonnet",
                    "api_mode": "anthropic_messages",
                    "base_url": "stub://local/claude",
                    "credential_ref": "stub-key",
                    "supports_reasoning": True,
                    "priority": 1,
                    "enabled": True,
                },
            ]
        )
        settings = RuntimeSettingRepository()
        settings.set_active_llm_endpoint_id("glm")
        settings.set("think_level", "xhigh")

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings,
        )

        self.assertEqual(runtime.think_level, "max")
        self.assertEqual(settings.get_think_level("glm"), "max")
        self.assertIsNone(settings.get_legacy_think_level())
        self.assertEqual(
            [choice["id"] for choice in runtime.thinking_status()["choices"]],
            ["off", "high", "max"],
        )

        runtime.set_active_endpoint("claude")
        self.assertEqual(runtime.think_level, "medium")
        self.assertEqual(runtime.set_think_level("minimal"), "low")
        runtime.set_active_endpoint("glm")
        self.assertEqual(runtime.think_level, "max")
        self.assertEqual(settings.get_think_level("claude"), "low")

    def test_llm_runtime_injects_think_level_into_request_metadata(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "stub_endpoint",
                    "provider": "zhipu",
                    "model_id": "glm-5.2",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/llm",
                    "credential_ref": "stub-key",
                    "context_window": 10000,
                    "max_output_tokens": 1200,
                    "supports_reasoning": True,
                    "capabilities_blob": {
                        "supports_thinking": True,
                        "thinking_contract": {
                            "default": "high",
                            "choices": [
                                "off",
                                {"id": "high", "label": "focused", "aliases": ["deep"]},
                            ],
                        }
                    },
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )
        settings_repository.set_think_level("stub_endpoint", "deep")
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
        )

        runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "system", "content": "hello"}],
                max_output_tokens=256,
                metadata={"origin": "test"},
            )
        )

        self.assertIsNotNone(runtime.last_request)
        self.assertEqual(runtime.last_request.metadata["think_level"], "high")

    def test_llm_runtime_injects_default_timeout_into_effective_request(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "stub_endpoint",
                    "provider": "stub",
                    "model_id": "stub-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/llm",
                    "credential_ref": "stub-key",
                    "context_window": 10000,
                    "max_output_tokens": 1200,
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )

        class CaptureInvoker:
            def __init__(self) -> None:
                self.requests: list[CanonicalLLMRequest] = []

            def invoke(self, endpoint, request: CanonicalLLMRequest):  # type: ignore[no-untyped-def]
                self.requests.append(request)
                return CanonicalLLMOutcome(text="ok", finish_reason=LLMFinishReason.STOP)

        invoker = CaptureInvoker()
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=invoker,
            config=RuntimeConfig(llm_request_timeout_seconds=37.0),
        )

        runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=256,
            )
        )

        self.assertEqual(invoker.requests[0].metadata["timeout_seconds"], 37.0)

    def test_llm_runtime_preserves_longer_explicit_request_timeout(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "stub_endpoint",
                    "provider": "stub",
                    "model_id": "stub-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/llm",
                    "credential_ref": "stub-key",
                    "context_window": 10000,
                    "max_output_tokens": 1200,
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )

        class CaptureInvoker:
            def __init__(self) -> None:
                self.requests: list[CanonicalLLMRequest] = []

            def invoke(self, endpoint, request: CanonicalLLMRequest):  # type: ignore[no-untyped-def]
                self.requests.append(request)
                return CanonicalLLMOutcome(text="ok", finish_reason=LLMFinishReason.STOP)

        invoker = CaptureInvoker()
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=invoker,
            config=RuntimeConfig(llm_request_timeout_seconds=300.0),
        )

        runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=256,
                metadata={"timeout_seconds": 3000.0},
            )
        )

        self.assertEqual(invoker.requests[0].metadata["timeout_seconds"], 3000.0)

    def test_compose_runtime_loads_first_party_sqlite_vec_plugin_via_plugin_host(self) -> None:
        self.wizard.seed_defaults(self.registration)

        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        records = handle.plugin_host.list_plugins()
        sqlite_record = next(item for item in records if item["plugin_id"] == "sqlite_vec_l3")
        self.assertEqual(sqlite_record["source"], "first_party")
        self.assertTrue(sqlite_record["attached"])
        self.assertEqual(handle.memory_service.l3_selector.active_provider_id, "sqlite_vec_l3")
        self.assertIn("sqlite_vec_l3", handle.core.context.execution_runtime.l3_plugin_registry.plugins)

    def test_wizard_seeds_default_web_providers_and_active_settings(self) -> None:
        self.wizard.seed_defaults(self.registration)

        search_records = WebSearchProviderRepository().list_all()
        fetch_records = WebFetchProviderRepository().list_all()
        settings = RuntimeSettingRepository()

        self.assertEqual(
            [item.provider_id for item in search_records],
            ["brave_search_default", "duckduckgo_search_default"],
        )
        self.assertEqual(
            [item.provider_id for item in fetch_records],
            ["playwright_fetch_default", "plain_http_fetch_default"],
        )
        self.assertEqual(settings.get("active_web_search_provider_id"), "brave_search_default")
        self.assertEqual(settings.get("active_web_fetch_provider_id"), "playwright_fetch_default")

    def test_compose_runtime_loads_first_party_web_plugins_and_default_tools(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        records = handle.plugin_host.list_plugins()
        plugin_ids = {item["plugin_id"] for item in records if item["source"] == "first_party" and item["attached"]}
        tool_names = {item["function"]["name"] for item in handle.core._build_llm_tool_contracts()}

        self.assertIn("web_search", plugin_ids)
        self.assertIn("web_fetch", plugin_ids)
        self.assertIn("mcp", plugin_ids)
        self.assertIsNotNone(handle.core.context.module_registry.get("web_search"))
        self.assertIsNotNone(handle.core.context.module_registry.get("web_fetch"))
        self.assertIsNotNone(handle.core.context.module_registry.get("mcp"))
        descriptors = handle.core.context.capability_registry.descriptors
        canonical_paths = {descriptor.canonical_path for descriptor in descriptors.values()}
        self.assertIn("op_web_search", canonical_paths)
        self.assertIn("op_web_read", canonical_paths)
        self.assertIn("op_web_screenshot", canonical_paths)
        self.assertIn("mcp_image_prepare", handle.core.context.capability_registry.descriptors)
        self.assertIn("web_search_show", handle.core.context.capability_registry.descriptors)
        self.assertIn("web_fetch_show", handle.core.context.capability_registry.descriptors)
        self.assertIn("mcp_show", handle.core.context.capability_registry.descriptors)
        self.assertTrue(
            any(name.startswith("web_search_provider_set_config") for name in handle.core.context.capability_registry.descriptors)
        )
        self.assertTrue(
            any(name.startswith("web_fetch_provider_set_config") for name in handle.core.context.capability_registry.descriptors)
        )
        self.assertIn("search_web", tool_names)
        self.assertIn("read_web", tool_names)
        self.assertNotIn("screenshot_web", tool_names)

    def test_compose_runtime_loads_minion_as_first_party_builtin_plugin(self) -> None:
        if not _local_sidecar_bind_available():
            self.skipTest("local socket binding is unavailable in this test sandbox")
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        records = handle.plugin_host.list_plugins()
        minion_record = next(item for item in records if item["plugin_id"] == "minion")
        self.assertEqual(minion_record["source"], "first_party")
        self.assertTrue(minion_record["attached"])
        self.assertIsNotNone(handle.core.context.module_registry.get("minion"))
        self.assertIn("minion_start_workflow", handle.core.context.capability_registry.descriptors)
        self.assertIn("minion_task_status", handle.core.context.capability_registry.descriptors)
        self.assertNotIn("minion_dispatch_workflow", handle.core.context.capability_registry.descriptors)
        search = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="op_tool_search", args={"query": "start minion workflow"})
        )
        minion_hit = self._find_search_hit_by_alias(handle.core, search, "minion_start_workflow")
        self.assertEqual(minion_hit["alias"], "minion_start_workflow")
        self.assertEqual(minion_hit["module_id"], "minion")
        self.assertIn("input_shape", minion_hit)

    def test_stub_runtime_provisions_builtin_plugins_for_fresh_compose(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_stub_builtin_test_"))
        wizard = WizardService()
        provisioned = wizard.provision_stub_runtime(root)
        try:
            handle = self._compose_runtime(
                wizard=wizard,
                registration=provisioned.registration,
                database=provisioned.database,
            )
            self.assertIn("minion_start_workflow", handle.core.context.capability_registry.descriptors)
            self.assertNotIn("minion_dispatch_workflow", handle.core.context.capability_registry.descriptors)
            search = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="op_tool_search", args={"query": "start minion workflow"})
            )
            minion_hit = self._find_search_hit_by_alias(handle.core, search, "minion_start_workflow")
            self.assertEqual(minion_hit["alias"], "minion_start_workflow")
            self.assertEqual(minion_hit["module_id"], "minion")
            self.assertIn("input_shape", minion_hit)
        finally:
            provisioned.database.close()
            shutil.rmtree(root, ignore_errors=True)

    def test_plugin_host_rescan_discovers_third_party_bundle_but_does_not_import_it(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        plugin_root = self.registration.runtime.runtime_root / "plugins" / "community" / "demo_plugin"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / "plugin.toml").write_text(
            "\n".join(
                [
                    'plugin_id = "demo_plugin"',
                    'entrypoint = "demo_plugin.runtime"',
                    'version = "0.1.0"',
                    "enabled_by_default = true",
                ]
            ),
            encoding="utf-8",
        )

        result = handle.plugin_host.rescan()
        discovered = PluginBundleRepository().get("demo_plugin")

        self.assertEqual(result["third_party_discovered"], 1)
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.last_load_status, "discovered")
        self.assertFalse(discovered.attached)

    def test_plugin_host_rescan_and_attach_new_first_party_attaches_new_builtin_plugin(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        builtin_root = self.runtime_root / "builtin_plugins"
        plugin_root = builtin_root / "demo_builtin"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / "plugin.toml").write_text(
            "\n".join(
                [
                    'plugin_id = "demo_builtin"',
                    'entrypoint = "demo_builtin.runtime"',
                    'version = "0.1.0"',
                    "enabled_by_default = true",
                ]
            ),
            encoding="utf-8",
        )
        (plugin_root / "__init__.py").write_text("", encoding="utf-8")
        (plugin_root / "runtime.py").write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "from dataclasses import dataclass",
                    "",
                    "from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle",
                    "",
                    "@dataclass",
                    "class DemoBundle:",
                    '    plugin_id: str = \"demo_builtin\"',
                    '    version: str = \"0.1.0\"',
                    "",
                    "    def register_with_core(self, context):",
                    "        handle = ModuleHandle(module_id=\"demo_builtin\", tier=MODULE_TIER_DETACHABLE, detachable=True)",
                    "        context.register_module(handle)",
                    "        return handle",
                    "",
                    "def build_plugin():",
                    "    return DemoBundle()",
                ]
            ),
            encoding="utf-8",
        )
        handle.plugin_host.builtin_root = builtin_root
        sys.path.insert(0, str(builtin_root))
        try:
            result = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_rescan_and_attach_new_first_party")
            )
        finally:
            sys.path.remove(str(builtin_root))

        self.assertEqual(result.status, "ok")
        self.assertIn("demo_builtin", result.structured["new_first_party_plugins"])
        self.assertIn("demo_builtin", result.structured["attached_new_first_party_plugins"])
        self.assertEqual(result.structured["attach_errors"], {})
        self.assertIsNotNone(handle.core.context.module_registry.get("demo_builtin"))
        records = handle.plugin_host.list_plugins()
        demo_record = next(item for item in records if item["plugin_id"] == "demo_builtin")
        self.assertTrue(demo_record["attached"])

    def test_plugin_attach_refreshes_import_cache_and_recompiles_capabilities(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        builtin_root = self.runtime_root / "builtin_reload_plugins"
        plugin_root = builtin_root / "demo_reload"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / "plugin.toml").write_text(
            "\n".join(
                [
                    'plugin_id = "demo_reload"',
                    'entrypoint = "demo_reload.runtime"',
                    'version = "0.1.0"',
                    "enabled_by_default = true",
                    'reload_modules = ["demo_reload.impl"]',
                ]
            ),
            encoding="utf-8",
        )
        (plugin_root / "__init__.py").write_text("", encoding="utf-8")

        def write_impl(value: str) -> None:
            (plugin_root / "impl.py").write_text(f"VALUE = '{value}'\n", encoding="utf-8")

        def write_runtime() -> None:
            (plugin_root / "runtime.py").write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "from dataclasses import dataclass",
                        "from demo_reload.impl import VALUE",
                        "from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle",
                        "from pal.execution import CapabilityCall, CapabilityResult",
                        "from pal.shared import OPERATION_NAMESPACE, capability_action, capability_node",
                        "",
                        "@capability_node(namespace=OPERATION_NAMESPACE, scope='demo_reload', kind='module', source='test', target_kind='module')",
                        "class DemoProvider:",
                        "    @capability_action(namespace=OPERATION_NAMESPACE, scope='demo_reload', family='operation', action_name='ping', aliases=('demo_reload_ping',))",
                        "    def ping(self, call: CapabilityCall) -> CapabilityResult:",
                        "        _ = call",
                        "        return CapabilityResult(status='ok', text=VALUE, llm_text=VALUE)",
                        "",
                        "@dataclass",
                        "class DemoBundle:",
                        "    plugin_id: str = 'demo_reload'",
                        "    version: str = '0.1.0'",
                        "    def register_with_core(self, context):",
                        "        handle = ModuleHandle(module_id='demo_reload', tier=MODULE_TIER_DETACHABLE, detachable=True, introspection_provider=DemoProvider())",
                        "        context.register_module(handle)",
                        "        return handle",
                        "",
                        "def build_plugin():",
                        "    return DemoBundle()",
                    ]
                ),
                encoding="utf-8",
            )

        write_impl("v1")
        write_runtime()
        handle.plugin_host.builtin_root = builtin_root
        sys.path.insert(0, str(builtin_root))
        try:
            result = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_rescan_and_attach_new_first_party")
            )
            self.assertEqual(result.status, "ok")
            first = handle.core.context.execution_runtime.execute(CapabilityCall(name="demo_reload_ping"))
            self.assertEqual(first.text, "v1")

            detached = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_detach", args={"plugin_id": "demo_reload"})
            )
            self.assertEqual(detached.status, "ok")

            time.sleep(1.1)
            write_impl("v2")
            write_runtime()
            attached = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_attach", args={"plugin_id": "demo_reload"})
            )
            self.assertEqual(attached.status, "ok")
            second = handle.core.context.execution_runtime.execute(CapabilityCall(name="demo_reload_ping"))
            self.assertEqual(second.text, "v2")
            self.assertIn("demo_reload_ping", handle.core.context.capability_registry.descriptors)
            self.assertNotIn("demo_reload_operation_ping", handle.core.context.capability_registry.descriptors)
        finally:
            if str(builtin_root) in sys.path:
                sys.path.remove(str(builtin_root))

    def test_builtin_plugin_manifests_declare_owned_reload_modules(self) -> None:
        builtin_root = Path(__file__).resolve().parents[1] / "src" / "pal" / "plugins_builtin"
        expected = {
            "lsp": "pal.lsp",
            "mcp": "pal.mcp",
            "minion": "pal.minion",
            "sqlite_vec_l3": "pal.plugins.l3",
            "web_fetch": "pal.web_fetch",
            "web_search": "pal.web_search",
        }

        for plugin_id, prefix in expected.items():
            payload = tomllib.loads((builtin_root / plugin_id / "plugin.toml").read_text(encoding="utf-8"))
            self.assertIn(prefix, payload.get("reload_modules", []), plugin_id)

    def test_builtin_plugins_detach_attach_refreshes_owned_module_caches(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        runtime = handle.core.context.execution_runtime
        initial_registry = handle.core.context.capability_registry
        expectations = {
            "lsp": ("pal.lsp", "lsp_show"),
            "mcp": ("pal.mcp", "mcp_show"),
            "minion": ("pal.minion", "minion_start_workflow"),
            "sqlite_vec_l3": ("pal.plugins.l3", "memory_provider_show__sqlite_vec_l3"),
            "web_fetch": ("pal.web_fetch", "web_fetch_show"),
            "web_search": ("pal.web_search", "web_search_show"),
        }
        owned_prefixes = tuple(prefix for prefix, _ in expectations.values())
        wrapper_prefixes = tuple(f"pal.plugins_builtin.{plugin_id}" for plugin_id in expectations)
        hot_reload_prefixes = (*owned_prefixes, *wrapper_prefixes)
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in hot_reload_prefixes)
        }

        try:
            for plugin_id, (reload_prefix, capability_name) in expectations.items():
                self.assertIn(capability_name, handle.core.context.capability_registry.descriptors, plugin_id)
                detached = runtime.execute(CapabilityCall(name="plugin_detach", args={"plugin_id": plugin_id}))
                self.assertEqual(detached.status, "ok", plugin_id)
                self.assertIn(capability_name, initial_registry.descriptors, plugin_id)
                self.assertNotIn(capability_name, handle.core.context.capability_registry.descriptors, plugin_id)

                probe_name = f"{reload_prefix}.__pal_hot_reload_probe__"
                sys.modules[probe_name] = types.ModuleType(probe_name)
                attached = runtime.execute(CapabilityCall(name="plugin_attach", args={"plugin_id": plugin_id}))

                self.assertEqual(attached.status, "ok", plugin_id)
                self.assertNotIn(probe_name, sys.modules, plugin_id)
                self.assertIn(capability_name, handle.core.context.capability_registry.descriptors, plugin_id)
                record = next(item for item in handle.plugin_host.list_plugins() if item["plugin_id"] == plugin_id)
                self.assertTrue(record["attached"], plugin_id)
        finally:
            asyncio.run(handle.stop_async())
            for name in list(sys.modules):
                if any(name == prefix or name.startswith(f"{prefix}.") for prefix in hot_reload_prefixes):
                    sys.modules.pop(name, None)
            sys.modules.update(original_modules)

    def test_core_reattach_delegates_plugin_owned_module_to_refresh_attach(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        initial_registry = handle.core.context.capability_registry
        prefixes = ("pal.minion", "pal.plugins_builtin.minion")
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
        }
        try:
            old_handle = handle.core.context.module_registry.require("minion")
            old_provider = old_handle.introspection_provider
            old_manager = old_handle.ports["minion"]
            old_pid = int(old_manager._require_manager()["manager_pid"])
            old_source = handle.core.context.event_source_registry.sources["minion.manager"]
            old_handler = handle.core.context.event_handler_registry.by_module["minion"][0][1]

            detached = handle.core.detach_module("minion")

            self.assertEqual(detached, "ok")
            self.assertIn("minion_start_workflow", initial_registry.descriptors)
            self.assertNotIn("minion_start_workflow", handle.core.context.capability_registry.descriptors)
            self.assertNotIn("minion.manager", handle.core.context.event_source_registry.sources)
            self.assertNotIn("minion", handle.core.context.event_handler_registry.by_module)
            self.assertFalse(old_manager.client.socket_path.exists())
            record = next(item for item in handle.plugin_host.list_plugins() if item["plugin_id"] == "minion")
            self.assertFalse(record["attached"])

            probe_name = "pal.minion.__pal_hot_reload_probe__"
            sys.modules[probe_name] = types.ModuleType(probe_name)

            reattached = handle.core.reattach_module("minion")

            self.assertEqual(reattached, "ok")
            self.assertNotIn(probe_name, sys.modules)
            self.assertIn("minion_start_workflow", handle.core.context.capability_registry.descriptors)
            new_handle = handle.core.context.module_registry.require("minion")
            new_manager = new_handle.ports["minion"]
            new_pid = int(new_manager._require_manager()["manager_pid"])
            new_source = handle.core.context.event_source_registry.sources["minion.manager"]
            new_handler = handle.core.context.event_handler_registry.by_module["minion"][0][1]
            self.assertIsNot(new_handle.introspection_provider, old_provider)
            self.assertNotEqual(new_pid, old_pid)
            self.assertIsNot(new_source, old_source)
            self.assertIsNot(new_handler, old_handler)
            record = next(item for item in handle.plugin_host.list_plugins() if item["plugin_id"] == "minion")
            self.assertTrue(record["attached"])
        finally:
            asyncio.run(handle.stop_async())
            for name in list(sys.modules):
                if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
                    sys.modules.pop(name, None)
            sys.modules.update(original_modules)

    def test_plugins_module_publishes_management_capabilities_and_can_detach_first_party_plugin(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        try:
            self.assertIn("plugins_show", handle.core.context.capability_registry.descriptors)
            self.assertIn("plugin_detach", handle.core.context.capability_registry.descriptors)
            self.assertIn("memory_provider_show__sqlite_vec_l3", handle.core.context.capability_registry.descriptors)

            detached = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_detach", args={"plugin_id": "sqlite_vec_l3"})
            )

            self.assertEqual(detached.status, "ok")
            self.assertNotIn("memory_provider_show__sqlite_vec_l3", handle.core.context.capability_registry.descriptors)
        finally:
            asyncio.run(handle.stop_async())

    def test_plugin_detach_runs_module_cleanup_callbacks(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        module = handle.core.context.module_registry.require("l3.sqlite_vec_l3")
        calls: list[str] = []
        module.cleanup_callbacks.append(lambda: calls.append("cleanup"))

        detached = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="plugin_detach", args={"plugin_id": "sqlite_vec_l3"})
        )

        self.assertEqual(detached.status, "ok")
        self.assertEqual(calls, ["cleanup"])
        self.assertEqual(module.cleanup_callbacks, [])

    def test_plugin_attach_detach_lifecycle_works_end_to_end(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        runtime = handle.core.context.execution_runtime
        initial_registry = handle.core.context.capability_registry
        plugin_host = handle.plugin_host

        # Verify sqlite_vec_l3 starts attached with capabilities
        l3_caps_before = [name for name in initial_registry.descriptors if "sqlite_vec_l3" in name]
        self.assertTrue(len(l3_caps_before) > 0, "L3 plugin should have capabilities on boot")
        self.assertIsNotNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # DETACH
        detached = runtime.execute(
            CapabilityCall(name="plugin_detach", args={"plugin_id": "sqlite_vec_l3"})
        )
        self.assertEqual(detached.status, "ok")

        # Capabilities withdrawn
        l3_caps_after_detach = [
            name for name in handle.core.context.capability_registry.descriptors if "sqlite_vec_l3" in name
        ]
        self.assertEqual(len(l3_caps_after_detach), 0, "All L3 capabilities should be withdrawn after detach")

        # Provider ref removed
        self.assertIsNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # Record shows detached
        records = plugin_host.list_plugins()
        l3_record = next(r for r in records if r["plugin_id"] == "sqlite_vec_l3")
        self.assertFalse(l3_record["attached"])

        # RE-ATTACH
        attached = runtime.execute(
            CapabilityCall(name="plugin_attach", args={"plugin_id": "sqlite_vec_l3"})
        )
        self.assertEqual(attached.status, "ok")

        # Capabilities restored
        l3_caps_after_reattach = [
            name for name in handle.core.context.capability_registry.descriptors if "sqlite_vec_l3" in name
        ]
        self.assertEqual(len(l3_caps_after_reattach), len(l3_caps_before), "All L3 capabilities should be restored after re-attach")

        # Provider ref restored
        self.assertIsNotNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # Record shows attached
        records2 = plugin_host.list_plugins()
        l3_record2 = next(r for r in records2 if r["plugin_id"] == "sqlite_vec_l3")
        self.assertTrue(l3_record2["attached"])

    def test_memory_l3_regression_build_pack_uses_builtin_sqlite_vec_provider(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        commit = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_write",
                args={
                    "target_id": "sqlite_vec_l3",
                    "kind": "fact",
                    "scope": "system",
                    "title": "Redis cache",
                    "summary": "Redis stores hot cache entries.",
                    "search_text": "Redis is used as a hot cache layer storing frequently accessed application data for low-latency retrieval.",
                    "topics": ["redis", "cache"],
                },
            )
        )
        pack = handle.memory_service.build_pack(MemoryPackRequest(turn_kind="chat"))

        self.assertEqual(commit.status, "ok")
        self.assertEqual(handle.memory_service.l3_selector.active_provider_id, "sqlite_vec_l3")

    def test_memory_l3_regression_capability_paths_round_trip_commit_recall_correct(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        committed = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_write",
                args={
                    "kind": "case",
                    "scope": "task",
                    "task_id": "task-1",
                    "summary": "Recovered the minion after memory pressure crash.",
                    "search_text": "Minion crashed under memory pressure. Restarted the minion and reduced concurrency. Queue drain recovered and latency normalized.",
                    "situation_text": "Minion crashed under memory pressure",
                    "task_text": "Stabilize the minion",
                    "action_text": "Restarted it and reduced concurrency",
                    "result_text": "Latency normalized",
                    "topics": ["minion", "stability"],
                },
            )
        )
        mem_ref = committed.structured["mem_ref"]

        recalled = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_recall",
                args={
                    "queries": ["minion memory pressure stabilize"],
                    "limit": 4,
                },
            )
        )
        recalled_origin = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_recall",
                args={
                    "queries": ["minion memory pressure stabilize"],
                    "limit": 4,
                    "view": "origin",
                },
            )
        )
        corrected = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_update",
                args={
                    "mem_ref": mem_ref,
                    "summary": "Recovered the minion after memory pressure.",
                    "topics": ["minion", "recovery"],
                },
            )
        )
        inventory = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="memory_provider_inventory__sqlite_vec_l3",
                args={"target_id": "sqlite_vec_l3"},
            )
        )

        self.assertEqual(committed.status, "ok")
        self.assertEqual(recalled.status, "ok")
        self.assertEqual(recalled_origin.status, "ok")
        self.assertEqual(corrected.status, "ok")
        self.assertEqual(recalled.structured["hit_count"], 1)
        self.assertEqual(recalled.structured["hits_preview"][0]["mem_ref"], mem_ref)
        self.assertEqual(recalled.structured["hits_preview"][0]["summary"], "Recovered the minion after memory pressure crash.")
        self.assertEqual(recalled.structured["view"], "summary")
        self.assertNotIn("hits", recalled.structured)
        self.assertNotIn("projected_entries", recalled.structured)
        self.assertNotIn("projected_entries", recalled.llm_text)
        self.assertNotIn("Restarted the minion and reduced concurrency.", recalled.llm_text)
        self.assertIn("Recovered the minion after memory pressure crash.", recalled.llm_text)
        self.assertEqual(recalled_origin.structured["view"], "origin")
        self.assertEqual(recalled_origin.structured["hit_count"], 1)
        self.assertEqual(recalled_origin.structured["hits_preview"][0]["mem_ref"], mem_ref)
        self.assertIn("Restarted the minion", recalled_origin.structured["hits_preview"][0]["search_text"])
        self.assertNotIn("hits", recalled_origin.structured)
        self.assertNotIn("projected_entries", recalled_origin.structured)
        self.assertIn("Restarted the minion and reduced concurrency.", recalled_origin.llm_text)
        self.assertNotIn("projected_entries", recalled_origin.llm_text)
        self.assertEqual(inventory.status, "ok")
        self.assertEqual(inventory.structured["provider_id"], "sqlite_vec_l3")
        self.assertIn(mem_ref, handle.memory_service.l2_store.items)
        self.assertIn(mem_ref, handle.memory_service.l2_store.heat_registry)
        self.assertEqual(handle.memory_service.l2_store.items[mem_ref].summary, "Recovered the minion after memory pressure.")

    def test_sqlite_vec_l3_commit_truth_topics_and_pending_index(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())

        result = provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis stores hot cache entries.",
                search_text="Redis stores hot cache entries.",
                canonical_key="cache.redis",
                topics=["cache", "redis"],
                payload={"category": "infra"},
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.metadata["index_status"], "pending")
        self.assertTrue(result.document_id.startswith("fact:"))
        inventory = provider.inspect()
        self.assertEqual(inventory["fact_count"], 1)
        self.assertEqual(inventory["pending_embeddings"], 1)
        self.assertIn(result.document_id, service.l2_store.items)
        self.assertIn(result.document_id, service.l2_store.heat_registry)

    def test_sqlite_vec_l3_refresh_indexes_and_vector_recall(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())
        provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis cache keeps hot application data fast.",
                search_text="Redis cache keeps hot application data fast.",
                topics=["cache", "redis"],
            )
        )
        provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Kafka stream",
                summary="Kafka handles event streaming.",
                search_text="Kafka handles event streaming.",
                topics=["stream", "kafka"],
            )
        )

        refreshed = provider.refresh_indexes(limit=8)
        recall = provider.recall(
            MemoryQuery(
                level="deep",
                queries=["redis cache"],
                limit=4,
            )
        )

        self.assertGreaterEqual(refreshed["refreshed"], 2)
        self.assertEqual(recall.hits[0]["title"], "Redis cache")
        self.assertEqual(recall.projected_entries[0].source_kind, "l3_recall")

    def test_sqlite_vec_l3_recall_degrades_to_fts_and_topics_when_vector_unavailable(self) -> None:
        class BrokenEmbedder(HashingEmbedder):
            def embed_query(self, text: str) -> list[float]:
                raise RuntimeError("query-embed-disabled")

            def embed_document(self, text: str) -> list[float]:
                raise RuntimeError("doc-embed-disabled")

        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=BrokenEmbedder())
        provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis stores hot cache entries.",
                search_text="Redis stores hot cache entries.",
                topics=["cache", "redis"],
            )
        )

        by_fts = provider.recall(MemoryQuery(queries=["redis"], limit=4))
        by_topic = provider.recall(MemoryQuery(queries=[], topic_scope=["cache"], limit=4))

        self.assertEqual(by_fts.hits[0]["title"], "Redis cache")
        self.assertEqual(by_topic.hits[0]["title"], "Redis cache")

    def test_sqlite_vec_l3_recall_uses_relaxed_lexical_candidates_when_vector_unavailable(self) -> None:
        class BrokenEmbedder(HashingEmbedder):
            def embed_query(self, text: str) -> list[float]:
                raise RuntimeError("query-embed-disabled")

            def embed_document(self, text: str) -> list[float]:
                raise RuntimeError("doc-embed-disabled")

        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=BrokenEmbedder())
        result = provider.commit(
            L3CommitRequest(
                kind="case",
                scope="task",
                task_id="task-1",
                title="Recover minion",
                summary="Recovered the minion after memory pressure crash.",
                search_text="Minion crashed under memory pressure. Restarted the minion and reduced concurrency. Queue drain recovered and latency normalized.",
                situation_text="Minion crashed under memory pressure",
                task_text="Stabilize the minion",
                action_text="Restarted it and reduced concurrency",
                result_text="Latency normalized",
                topics=["minion", "stability"],
            )
        )

        recall = provider.recall(MemoryQuery(queries=["minion memory pressure stabilize"], limit=4))

        self.assertEqual(recall.hits[0]["document_id"], result.document_id)
        self.assertEqual(recall.metadata["retrieval_mode"], "lexical")
        self.assertTrue(recall.metadata["degraded"])
        self.assertGreaterEqual(recall.metadata["candidate_sources"]["fts_jieba"], 1)

    def test_sqlite_vec_l3_recall_supports_cjk_jieba_and_short_like_fallback(self) -> None:
        class BrokenEmbedder(HashingEmbedder):
            def embed_query(self, text: str) -> list[float]:
                raise RuntimeError("query-embed-disabled")

            def embed_document(self, text: str) -> list[float]:
                raise RuntimeError("doc-embed-disabled")

        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=BrokenEmbedder())
        provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="回复风格",
                summary="用户喜欢简洁中文回复。",
                search_text="用户喜欢简洁中文回复风格，避免冗长解释。",
                topics=["偏好", "回复"],
            )
        )

        jieba_hit = provider.recall(MemoryQuery(queries=["简洁中文回复"], limit=4))
        short_hit = provider.recall(MemoryQuery(queries=["简洁"], limit=4))

        self.assertEqual(jieba_hit.hits[0]["title"], "回复风格")
        self.assertGreaterEqual(jieba_hit.metadata["candidate_sources"]["fts_jieba"], 1)
        self.assertEqual(short_hit.hits[0]["title"], "回复风格")
        self.assertGreaterEqual(short_hit.metadata["candidate_sources"]["like"], 1)

    def test_sqlite_vec_l3_prefers_sqlite_vec_candidates_before_python_fallback(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())

        provider.repository.query_vector_candidates_sqlite_vec = lambda **kwargs: {"fact:redis": 0.91}  # type: ignore[method-assign]

        def _unexpected_python_scan(**kwargs):
            raise AssertionError("python fallback should not run when sqlite-vec returns candidates")

        provider.repository.list_vector_rows = _unexpected_python_scan  # type: ignore[method-assign]

        scores = provider._vector_candidates("redis cache", limit=4)

        self.assertEqual(scores, {"fact:redis": 0.91})

    def test_sqlite_vec_l3_recall_prefers_multi_source_agreement_over_single_source_peak(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())
        alpha = provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Alpha lexical only",
                summary="Alpha is strong only in lexical retrieval.",
                search_text="alpha lexical retrieval anchor",
                topics=["alpha"],
            )
        )
        beta = provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Beta cross source",
                summary="Beta aligns across lexical topic and vector retrieval.",
                search_text="beta retrieval alignment shared evidence",
                topics=["beta", "alignment"],
            )
        )
        gamma = provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Gamma vector only",
                summary="Gamma is strong only in vector retrieval.",
                search_text="gamma vector retrieval anchor",
                topics=["gamma"],
            )
        )
        alpha_id = alpha.document_id
        beta_id = beta.document_id
        gamma_id = gamma.document_id

        provider.refresh_indexes = lambda **kwargs: {"refreshed": 0, "vector_available": True}  # type: ignore[method-assign]
        provider.repository.collect_lexical_candidates = lambda *args, **kwargs: (  # type: ignore[method-assign]
            {
                str(alpha_id): 100.0,
                str(beta_id): 60.0,
            },
            {"fts_jieba": 2, "like": 0},
        )
        provider.repository.list_topic_candidates = lambda *args, **kwargs: {str(beta_id): 1.0}  # type: ignore[method-assign]
        provider._vector_candidates = lambda *args, **kwargs: {  # type: ignore[method-assign]
            str(gamma_id): 0.99,
            str(beta_id): 0.52,
        }

        recall = provider.recall(MemoryQuery(queries=["shared retrieval evidence"], topic_scope=["alignment"], limit=3))

        self.assertEqual(recall.hits[0]["title"], "Beta cross source")
        self.assertEqual(recall.metadata["fusion_strategy"], "ranked-hybrid-v1")
        self.assertGreater(recall.hits[0]["scores"]["source_count"], recall.hits[1]["scores"]["source_count"])

    def test_sqlite_vec_l3_case_indexes_st_and_returns_ar(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())
        result = provider.commit(
            L3CommitRequest(
                kind="case",
                scope="task",
                task_id="task-1",
                title="Restart flaky minion",
                summary="Restarted flaky minion after memory pressure crash.",
                search_text="Minion crashed after high memory pressure. Task was to stabilize the background minion. Restarted the minion and reduced concurrency. Queue drain recovered and latency normalized.",
                situation_text="Minion crashed after high memory pressure",
                task_text="Stabilize the background minion",
                action_text="Restarted the minion and reduced concurrency",
                result_text="Queue drain recovered and latency normalized",
                topics=["minion", "stability"],
            )
        )
        provider.refresh_indexes(limit=8)

        st_hit = provider.recall(MemoryQuery(queries=["minion high memory pressure stabilize"], limit=4))
        ar_only = provider.recall(MemoryQuery(queries=["latency normalized"], limit=4))

        self.assertEqual(st_hit.hits[0]["document_id"], result.document_id)
        self.assertIn("Action: Restarted the minion", st_hit.hits[0]["rendered"])
        self.assertGreaterEqual(len(ar_only.hits), 0)

    def test_sqlite_vec_l3_correct_marks_stale_and_updates_topics(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())
        result = provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis stores cache entries.",
                search_text="Redis stores cache entries.",
                topics=["cache"],
            )
        )
        provider.refresh_indexes(limit=8)

        corrected = provider.correct(
            L3CorrectRequest(
                document_id=result.document_id,
                summary="Redis stores hot cache entries in memory.",
                search_text="Redis stores hot cache entries in memory.",
                topics=["cache", "redis"],
                payload_patch={"updated": True},
            )
        )

        self.assertEqual(corrected.status, "ok")
        self.assertEqual(corrected.metadata["index_status"], "stale")
        inventory = provider.inspect()
        self.assertGreaterEqual(inventory["stale_embeddings"], 1)
        self.assertGreaterEqual(inventory["retryable_embeddings"], 1)

    def test_sqlite_vec_l3_correct_preserves_existing_topics_when_topics_not_provided(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())
        result = provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis stores cache entries.",
                search_text="Redis stores cache entries.",
                topics=["cache", "redis"],
            )
        )

        provider.correct(
            L3CorrectRequest(
                document_id=result.document_id,
                summary="Redis stores hot cache entries in memory.",
                search_text="Redis stores hot cache entries in memory for fast retrieval.",
            )
        )

        document = provider.repository.get_document(result.document_id)
        self.assertIsNotNone(document)
        assert document is not None
        self.assertIn("cache", document["search_text"].lower())
        self.assertIn("redis", document["search_text"].lower())
        self.assertEqual(provider.repository.list_document_topics(result.document_id), ["cache", "redis"])

    def test_sqlite_vec_l3_inventory_surfaces_failed_embedding_diagnostics(self) -> None:
        class BrokenEmbedder(HashingEmbedder):
            def embed_document(self, text: str) -> list[float]:
                raise RuntimeError("doc-embed-disabled")

        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=BrokenEmbedder())
        provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis stores cache entries.",
                search_text="Redis stores cache entries.",
                topics=["cache"],
            )
        )

        refreshed = provider.refresh_indexes(limit=8)
        inventory = provider.inspect()

        self.assertEqual(refreshed["refreshed"], 0)
        self.assertEqual(inventory["failed_embeddings"], 1)
        self.assertEqual(inventory["retryable_embeddings"], 1)
        self.assertEqual(len(inventory["recent_embedding_errors"]), 1)
        self.assertIn("doc-embed-disabled", inventory["recent_embedding_errors"][0]["last_error"])

    def test_sqlite_vec_l3_refresh_indexes_can_retry_failed_embeddings(self) -> None:
        class FlakyEmbedder(HashingEmbedder):
            def __init__(self) -> None:
                self.fail_once = True

            def embed_document(self, text: str) -> list[float]:
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("temporary-embed-error")
                return super().embed_document(text)

        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=FlakyEmbedder())
        provider.commit(
            L3CommitRequest(
                kind="fact",
                scope="system",
                title="Redis cache",
                summary="Redis stores cache entries.",
                search_text="Redis stores cache entries.",
                topics=["cache"],
            )
        )

        first = provider.refresh_indexes(limit=8)
        second = provider.refresh_indexes(limit=8, retry_failed=True)
        inventory = provider.inspect()

        self.assertEqual(first["refreshed"], 0)
        self.assertEqual(second["failed_retried"], 1)
        self.assertGreaterEqual(second["refreshed"], 1)
        self.assertEqual(inventory["failed_embeddings"], 0)
        self.assertEqual(inventory["ready_embeddings"], 1)

    def test_memory_service_promotes_commits_to_hot(self) -> None:
        service = MemoryService()
        provider = SQLiteVecL3Plugin(service=service, embedder=HashingEmbedder())
        service.l3_selector = L3ProviderSelector(resolver={provider.provider_id: provider}.get, active_provider_id=provider.provider_id)  # type: ignore[arg-type]

        for index in range(10):
            provider.commit(
                L3CommitRequest(
                    kind="fact",
                    scope="system",
                    title=f"Fact {index}",
                    summary=f"Summary {index}",
                    topics=[f"topic-{index}"],
                )
            )

        from pal.memory.contracts import L2HeatLevel
        hot_ids = [eid for eid, state in service.l2_store.heat_registry.items() if state.heat_level == L2HeatLevel.HOT]
        self.assertEqual(len(hot_ids), 10)

    def test_openai_chat_invoker_does_not_map_think_level_to_reasoning_effort_for_plain_openai_chat(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="plain-openai-chat",
            provider="openai_compatible",
            model_id="gpt-test",
            api_mode="openai_chat",
            base_url="https://example.test/v1",
            credential_ref="openai-compatible-prod",
            enabled=True,
        )
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=InMemorySecretStore())
        )

        kwargs, _ = invoker._build_completion_kwargs(
            endpoint,
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                metadata={"think_level": "deep"},
            ),
        )

        self.assertNotIn("reasoning_effort", kwargs)

    def test_llm_runtime_retries_primary_then_falls_back_to_next_endpoint(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "primary",
                    "provider": "stub",
                    "model_id": "primary-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/primary",
                    "credential_ref": "stub-primary",
                    "priority": 0,
                    "enabled": True,
                },
                {
                    "endpoint_id": "fallback",
                    "provider": "stub",
                    "model_id": "fallback-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/fallback",
                    "credential_ref": "stub-fallback",
                    "priority": 1,
                    "enabled": True,
                },
            ]
        )

        class RetryThenFallbackInvoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request):
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "primary":
                    raise RuntimeError("primary transport failed")
                return CanonicalLLMOutcome(
                    text=f"reply via {endpoint.endpoint_id}",
                    tool_calls=[],
                    finish_reason=LLMFinishReason.STOP,
                )

        invoker = RetryThenFallbackInvoker()
        events: list[dict[str, object]] = []
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=invoker,
            endpoint_retry_attempts=2,
            event_sink=events.append,
        )

        outcome = runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=256,
            )
        )

        self.assertEqual(invoker.calls, ["primary", "primary", "fallback"])
        self.assertEqual(outcome.text, "reply via fallback")
        self.assertEqual(runtime.last_endpoint_id, "fallback")
        self.assertEqual(runtime.last_model_id, "fallback-model")
        self.assertNotIn("think_level", runtime.last_request.metadata)
        self.assertEqual(runtime.last_request.metadata["endpoint_id"], "fallback")
        self.assertEqual(
            [event["phase"] for event in events],
            [
                "llm_endpoint_attempt_failed",
                "llm_endpoint_retry_scheduled",
                "llm_endpoint_attempt_failed",
                "llm_endpoint_exhausted",
                "llm_endpoint_fallback_started",
                "llm_endpoint_fallback_succeeded",
            ],
        )
        self.assertEqual(events[0]["endpoint_id"], "primary")
        self.assertEqual(events[0]["attempt"], 1)
        self.assertEqual(events[1]["next_attempt"], 2)
        self.assertEqual(events[3]["next_endpoint_id"], "fallback")
        self.assertEqual(events[-1]["endpoint_id"], "fallback")

    def test_llm_runtime_agenerate_does_not_block_event_loop(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "blocking",
                    "provider": "stub",
                    "model_id": "blocking-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/blocking",
                    "credential_ref": "stub-blocking",
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )

        class BlockingInvoker:
            def invoke(self, endpoint, request):
                _ = endpoint, request
                time.sleep(0.08)
                return CanonicalLLMOutcome(text="done", tool_calls=[], finish_reason=LLMFinishReason.STOP)

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=BlockingInvoker(),
        )

        async def scenario() -> CanonicalLLMOutcome:
            task = asyncio.create_task(
                runtime.agenerate(
                    CanonicalLLMRequest(
                        messages=[{"role": "user", "content": "hello"}],
                        max_output_tokens=64,
                    )
                )
            )
            started_at = time.monotonic()
            await asyncio.sleep(0.01)
            self.assertLess(time.monotonic() - started_at, 0.05)
            return await asyncio.wait_for(task, timeout=1.0)

        outcome = asyncio.run(scenario())

        self.assertEqual(outcome.text, "done")

    def test_llm_runtime_generate_stream_returns_normalized_events(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "streaming",
                    "provider": "stub",
                    "model_id": "streaming-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/streaming",
                    "credential_ref": "stub-streaming",
                    "priority": 0,
                    "enabled": True,
                    "supports_streaming": True,
                }
            ]
        )

        class StreamingInvoker:
            def invoke(self, endpoint, request):
                _ = endpoint
                _ = request
                return CanonicalLLMOutcome(text="unused", tool_calls=[], finish_reason=LLMFinishReason.STOP)

            def invoke_stream(self, endpoint, request):
                _ = endpoint
                _ = request
                return [
                    NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="hello "),
                    NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="world"),
                    NormalizedLLMStreamEvent(
                        event_kind=LLMStreamEventKind.DONE,
                        finish_reason=LLMFinishReason.STOP,
                        response_mode="chat",
                    ),
                ]

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=StreamingInvoker(),
        )

        events = runtime.generate_stream(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "stream me"}],
                max_output_tokens=128,
            )
        )

        self.assertEqual([event.event_kind for event in events], [LLMStreamEventKind.TEXT_DELTA, LLMStreamEventKind.TEXT_DELTA, LLMStreamEventKind.DONE])
        self.assertEqual("".join(event.text for event in events), "hello world")
        self.assertEqual(events[-1].finish_reason, LLMFinishReason.STOP)
        self.assertEqual(events[-1].response_mode, "chat")

    def test_openai_chat_invoker_stream_normalizes_reasoning_only_chunks(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="deepseek-like",
            provider="deepseek",
            model_id="deepseek/deepseek-chat",
            api_mode="openai_chat",
            base_url="https://api.deepseek.com/v1",
            credential_ref="deepseek-prod",
            enabled=True,
            supports_streaming=True,
        )

        class FakeChunk:
            def __init__(self, payload):
                self.payload = payload

            def to_dict(self):
                return dict(self.payload)

        class FakeResponse:
            def to_dict(self):
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "pong", "tool_calls": []},
                        }
                    ]
                }

        class FakeOpenAICompletions:
            calls = []

            @staticmethod
            def create(**kwargs):
                FakeOpenAICompletions.calls.append(dict(kwargs))
                if kwargs.get("stream"):
                    return [
                        FakeChunk(
                            {
                                "choices": [
                                    {
                                        "finish_reason": None,
                                        "delta": {"reasoning_content": "thinking", "content": None},
                                    }
                                ]
                            }
                        ),
                        FakeChunk(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "length",
                                        "delta": {"reasoning_content": "still thinking", "content": None},
                                    }
                                ]
                            }
                        ),
                    ]
                return FakeResponse()

        class FakeOpenAIClient:
            def __init__(self, **kwargs):
                self.client_kwargs = kwargs
                self.chat = types.SimpleNamespace(
                    completions=FakeOpenAICompletions(),
                )

        class FakeOpenAI:
            OpenAI = FakeOpenAIClient

        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=InMemorySecretStore())
        )
        request = CanonicalLLMRequest(
            messages=[{"role": "user", "content": "Reply with exactly: pong"}],
            max_output_tokens=32,
        )

        with patch.dict(sys.modules, {"openai": FakeOpenAI}):
            events = list(invoker.invoke_stream(endpoint, request))

        self.assertEqual(
            [event.event_kind for event in events],
            [LLMStreamEventKind.REASONING_DELTA, LLMStreamEventKind.REASONING_DELTA, LLMStreamEventKind.DONE],
        )
        self.assertEqual("".join(event.reasoning_text for event in events), "thinkingstill thinking")
        self.assertEqual(events[-1].finish_reason, "length")
        self.assertEqual(
            FakeOpenAICompletions.calls[0]["stream_options"],
            {"include_usage": True},
        )

    def test_openai_chat_response_parser_preserves_reasoning_text(self) -> None:
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=InMemorySecretStore())
        )

        class FakeResponse:
            def to_dict(self):
                return {
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 5,
                        "cost": 0.0125,
                    },
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "pong",
                                "reasoning_content": "thinking",
                                "tool_calls": [],
                            },
                        }
                    ]
                }

        outcome = invoker._parse_openai_chat_response(FakeResponse())

        self.assertEqual(outcome.text, "pong")
        self.assertEqual(outcome.reasoning_text, "thinking")
        self.assertEqual(outcome.provider_specific_fields["reasoning_content"], "thinking")
        self.assertEqual(outcome.input_tokens, 17)
        self.assertEqual(outcome.output_tokens, 5)
        self.assertEqual(outcome.cost, 0.0125)

    def test_anthropic_parser_and_renderer_preserve_thinking_blocks_for_tool_continuation(self) -> None:
        class FakeResponse:
            def to_dict(self):
                return {
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 23,
                        "output_tokens": 11,
                        "total_cost": 0.031,
                    },
                    "content": [
                        {"type": "thinking", "thinking": "hidden reasoning", "signature": "sig-1"},
                        {"type": "redacted_thinking", "data": "opaque"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "probe_tool",
                            "input": {"ok": True},
                        },
                    ],
                }

        outcome = _parse_anthropic_messages_response(FakeResponse())

        self.assertEqual(outcome.reasoning_text, "hidden reasoning")
        self.assertEqual(outcome.provider_specific_fields["reasoning_content"], "hidden reasoning")
        self.assertEqual(outcome.provider_specific_fields["anthropic_thinking_blocks"][0]["signature"], "sig-1")
        self.assertEqual(outcome.provider_specific_fields["anthropic_thinking_blocks"][1]["type"], "redacted_thinking")
        self.assertEqual(outcome.input_tokens, 23)
        self.assertEqual(outcome.output_tokens, 11)
        self.assertEqual(outcome.cost, 0.031)

        _, messages = chat_messages_to_anthropic_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "provider_specific_fields": outcome.provider_specific_fields,
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "probe_tool", "arguments": "{\"ok\": true}"},
                        }
                    ],
                }
            ]
        )

        blocks = messages[0]["content"]
        self.assertEqual([block["type"] for block in blocks], ["thinking", "redacted_thinking", "tool_use"])
        self.assertEqual(blocks[0]["signature"], "sig-1")

    def test_anthropic_renderer_coalesces_adjacent_user_turns(self) -> None:
        _, messages = chat_messages_to_anthropic_messages(
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "task"},
                {"role": "user", "content": "runtime reminder"},
                {
                    "role": "assistant",
                    "content": "use tools",
                    "tool_calls": [
                        {"id": "call_a", "type": "function", "function": {"name": "read_a", "arguments": "{}"}},
                        {"id": "call_b", "type": "function", "function": {"name": "read_b", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_a", "content": "a"},
                {"role": "tool", "tool_call_id": "call_b", "content": "b"},
            ]
        )

        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user"])
        self.assertEqual([block["type"] for block in messages[0]["content"]], ["text", "text"])
        self.assertEqual([block["type"] for block in messages[2]["content"]], ["tool_result", "tool_result"])

    def test_openai_chat_responses_parser_extracts_text_and_tool_calls(self) -> None:
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=InMemorySecretStore())
        )

        class FakeResponse:
            def to_dict(self):
                return {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "pong"}],
                        },
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "probe_alias",
                            "arguments": "{\"ok\": true}",
                        },
                    ]
                }

        outcome = invoker._parse_openai_responses_response(
            FakeResponse(),
            tool_name_aliases={"probe": "probe_alias"},
        )

        self.assertEqual(outcome.text, "pong")
        self.assertEqual(outcome.finish_reason, LLMFinishReason.TOOL_CALLS)
        self.assertEqual(outcome.tool_calls[0].name, "probe")
        self.assertEqual(outcome.tool_calls[0].args, {"ok": True})
        self.assertEqual(outcome.tool_calls[0].call_id, "call_1")

    def test_openai_chat_response_parser_rejects_empty_assistant_without_tool_calls(self) -> None:
        invoker = OpenAIChatEndpointInvoker(
            credentials=LLMCredentialResolver(secret_store=InMemorySecretStore())
        )

        class FakeResponse:
            def to_dict(self):
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "",
                                "tool_calls": [],
                            },
                        }
                    ]
                }

        with self.assertRaises(LLMEndpointInvocationError):
            invoker._parse_openai_chat_response(FakeResponse())

    def test_llm_runtime_treats_empty_assistant_without_tool_calls_as_endpoint_failure(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "empty-primary",
                    "provider": "stub",
                    "model_id": "empty-primary-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/empty-primary",
                    "credential_ref": "stub-empty-primary",
                    "priority": 0,
                    "enabled": True,
                },
                {
                    "endpoint_id": "fallback",
                    "provider": "stub",
                    "model_id": "fallback-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/fallback",
                    "credential_ref": "stub-fallback",
                    "priority": 1,
                    "enabled": True,
                },
            ]
        )

        class EmptyThenFallbackInvoker:
            def invoke(self, endpoint, request):
                _ = request
                if endpoint.endpoint_id == "empty-primary":
                    return CanonicalLLMOutcome(text="", tool_calls=[], finish_reason=LLMFinishReason.STOP)
                return CanonicalLLMOutcome(text="fallback reply", finish_reason=LLMFinishReason.STOP)

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=EmptyThenFallbackInvoker(),
            endpoint_retry_attempts=1,
        )

        outcome = runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=128,
            )
        )

        self.assertEqual(outcome.text, "fallback reply")
        self.assertEqual(runtime.last_endpoint_id, "fallback")

    def test_llm_runtime_treats_empty_stream_without_tool_calls_as_endpoint_failure(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "empty-primary",
                    "provider": "stub",
                    "model_id": "empty-primary-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/empty-primary",
                    "credential_ref": "stub-empty-primary",
                    "priority": 0,
                    "enabled": True,
                },
                {
                    "endpoint_id": "fallback",
                    "provider": "stub",
                    "model_id": "fallback-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/fallback",
                    "credential_ref": "stub-fallback",
                    "priority": 1,
                    "enabled": True,
                },
            ]
        )

        class EmptyThenFallbackStreamInvoker:
            def invoke_stream(self, endpoint, request):
                _ = request
                if endpoint.endpoint_id == "empty-primary":
                    return [NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.STOP)]
                return [
                    NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="fallback stream"),
                    NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason=LLMFinishReason.STOP),
                ]

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=EmptyThenFallbackStreamInvoker(),
            endpoint_retry_attempts=1,
        )

        events = runtime.generate_stream(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=128,
            )
        )

        self.assertEqual("".join(event.text for event in events), "fallback stream")
        self.assertEqual(runtime.last_endpoint_id, "fallback")

    def test_llm_runtime_returns_error_outcome_when_all_endpoints_fail(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "broken",
                    "provider": "stub",
                    "model_id": "broken-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/broken",
                    "credential_ref": "stub-broken",
                    "priority": 0,
                    "enabled": True,
                }
            ]
        )

        class AlwaysFailingInvoker:
            def invoke(self, endpoint, request):
                _ = endpoint
                _ = request
                raise RuntimeError("backend offline")

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=AlwaysFailingInvoker(),
            endpoint_retry_attempts=2,
        )

        outcome = runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=128,
            )
        )

        self.assertEqual(outcome.finish_reason, LLMFinishReason.ERROR)
        self.assertIn("kind=unknown", outcome.text)
        self.assertIn("type=RuntimeError", outcome.text)
        self.assertNotIn("backend offline", outcome.text)

    def test_llm_runtime_returns_compact_required_when_fallback_endpoint_window_is_tighter(self) -> None:
        endpoint_repository = LLMEndpointRepository()
        settings_repository = RuntimeSettingRepository()
        endpoint_repository.ensure_defaults(
            [
                {
                    "endpoint_id": "primary",
                    "provider": "stub",
                    "model_id": "primary-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/primary",
                    "credential_ref": "stub-primary",
                    "context_window": 10000,
                    "max_output_tokens": 1000,
                    "priority": 0,
                    "enabled": True,
                },
                {
                    "endpoint_id": "fallback-small",
                    "provider": "stub",
                    "model_id": "fallback-small-model",
                    "api_mode": "openai_chat",
                    "base_url": "stub://local/fallback-small",
                    "credential_ref": "stub-fallback-small",
                    "context_window": 1200,
                    "max_output_tokens": 512,
                    "priority": 1,
                    "enabled": True,
                },
            ]
        )

        class PrimaryFailsInvoker:
            def invoke(self, endpoint, request):
                _ = request
                if endpoint.endpoint_id == "primary":
                    raise RuntimeError("primary transport failed")
                return CanonicalLLMOutcome(text="unexpected", tool_calls=[], finish_reason=LLMFinishReason.STOP)

        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=PrimaryFailsInvoker(),
            endpoint_retry_attempts=1,
        )

        outcome = runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "x" * 2000}],
                max_output_tokens=700,
            )
        )

        self.assertEqual(outcome.finish_reason, LLMFinishReason.COMPACT_REQUIRED)
        self.assertEqual(outcome.preferred_endpoint_id, "fallback-small")
        self.assertEqual(outcome.preferred_model_id, "fallback-small-model")
        self.assertGreater(outcome.reserved_output_tokens, 0)
        self.assertGreater(outcome.target_input_budget, 0)

    def test_wizard_provisions_stub_runtime_before_bootstrap_composition(self) -> None:
        provisioned = self.wizard.provision_stub_runtime(self.runtime_root / "stub_runtime")
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=provisioned.registration,
            database=provisioned.database,
        )
        handle.database.close()

        self.assertEqual(handle.registration.runtime.db_path, handle.database.db_path)
        self.assertEqual(handle.wizard.registrations[-1], handle.registration)
        self.assertIsNotNone(handle.core.context.module_registry.get("core"))
        self.assertIsNotNone(handle.core.context.module_registry.get("channel"))
        self.assertIsNotNone(handle.core.context.module_registry.get("llm"))
        self.assertIsNotNone(handle.core.context.module_registry.get("memory"))
        self.assertIsNotNone(handle.core.context.module_registry.get("identity"))
        execution_runtime = handle.core.context.execution_runtime
        channel_list_path = execution_runtime.resolve_capability_address("channel_list")
        self.assertIsNotNone(execution_runtime.bound_action_index.get(channel_list_path, SINGLETON_TARGET))

    def test_compose_runtime_registers_seeded_socket_endpoint(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        endpoint = handle.channel_runtime.get_endpoint("socket_default")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.endpoint.channel_kind, "socket")
        self.assertEqual(Path(endpoint.endpoint.binding_key), self.registration.runtime.runtime_root / "pal.sock")

    def test_open_runtime_reattaches_recovery_socket_by_binding(self) -> None:
        from pal.runtime_app import open_runtime

        self.database.close()
        runtime_root = Path(tempfile.mkdtemp(prefix="pal_open_runtime_recovery_socket_"))
        handle = None
        try:
            wizard = WizardService()
            registration = wizard.provision_runtime(
                display_name="PalV2 Test",
                runtime_root=runtime_root,
                db_filename="pal.sqlite3",
                pal_entrypoint="pal.runtime.test",
            )
            database = wizard.create_database(registration)
            ChannelEndpointRepository().upsert(
                endpoint_id="sock-1",
                channel_kind="socket",
                binding_key=str(runtime_root / "pal.sock"),
                enabled=False,
                detached_at="2026-01-01T00:00:00+00:00",
                supports_typing=False,
                supports_receipt_marker=False,
                binding_metadata={},
                send_policy_blob={},
            )
            database.close()

            handle = open_runtime(runtime_root)

            record = ChannelEndpointRepository().get("sock-1")
            self.assertIsNotNone(record)
            self.assertTrue(record.enabled)
            self.assertIsNone(record.detached_at)
            self.assertIsNone(ChannelEndpointRepository().get("socket_default"))
            self.assertIsNotNone(handle.channel_runtime.get_endpoint("sock-1"))
        finally:
            if handle is not None:
                asyncio.run(handle.stop_async())
                handle.database.close()
            shutil.rmtree(runtime_root, ignore_errors=True)

    def test_compose_runtime_hydrates_channel_endpoints_inside_running_event_loop(self) -> None:
        self.wizard.seed_defaults(self.registration)

        async def run() -> None:
            handle = self._compose_runtime(
                wizard=self.wizard,
                registration=self.registration,
                database=self.database,
            )
            endpoint = handle.channel_runtime.get_endpoint("socket_default")
            self.assertIsNotNone(endpoint)
            await handle.stop_async()

        asyncio.run(run())

    def test_channel_endpoint_provider_reload_rebuilds_provider_without_detaching_channel_bus(self) -> None:
        self.wizard.seed_defaults(self.registration)
        ChannelEndpointRepository().upsert(
            endpoint_id="socket_aux",
            channel_kind="socket",
            binding_key=str(self.registration.runtime.runtime_root / "aux.sock"),
            enabled=True,
            supports_typing=False,
            supports_receipt_marker=False,
            binding_metadata={},
            send_policy_blob={},
        )
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        runtime = handle.core.context.execution_runtime
        old_endpoint = handle.channel_runtime.get_endpoint("socket_default")
        old_aux_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
        self.assertIsNotNone(old_endpoint)
        self.assertIsNotNone(old_aux_endpoint)
        self.assertIn("channel", handle.core.context.module_registry.modules)
        self.assertIn("channel_reload_provider", handle.core.context.capability_registry.descriptors)

        prefixes = ("pal.channel.factory", "pal.channel.endpoints.socket_endpoint")
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
        }
        probe_name = "pal.channel.endpoints.socket_endpoint.__pal_hot_reload_probe__"
        try:
            sys.modules[probe_name] = types.ModuleType(probe_name)

            result = runtime.execute(
                CapabilityCall(name="channel_reload_provider", args={"target_id": "socket_default"})
            )

            self.assertEqual(result.status, "ok")
            self.assertNotIn(probe_name, sys.modules)
            self.assertIn("pal.channel.endpoints.socket_endpoint", result.structured["reload_modules"])
            new_endpoint = handle.channel_runtime.get_endpoint("socket_default")
            self.assertIsNotNone(new_endpoint)
            self.assertIsNot(new_endpoint, old_endpoint)
            self.assertEqual(new_endpoint.endpoint.endpoint_id, "socket_default")
            self.assertIn("channel", handle.core.context.module_registry.modules)
            self.assertIn("channel_reload_provider", handle.core.context.capability_registry.descriptors)

            blocked = runtime.execute(
                CapabilityCall(name="channel_detach", args={"target_id": "socket_default"})
            )
            self.assertEqual(blocked.status, "invalid")
            self.assertEqual(blocked.structured["reason"], "recovery_socket_control_channel")
            self.assertIs(handle.channel_runtime.get_endpoint("socket_default"), new_endpoint)
            self.assertTrue(new_endpoint.attached)

            blocked_disable = runtime.execute(
                CapabilityCall(name="channel_disable", args={"target_id": "socket_default"})
            )
            self.assertEqual(blocked_disable.status, "invalid")
            self.assertEqual(blocked_disable.structured["reason"], "recovery_socket_control_channel")
            self.assertTrue(new_endpoint.enabled)

            detached = runtime.execute(
                CapabilityCall(name="channel_detach", args={"target_id": "socket_aux"})
            )
            self.assertEqual(detached.status, "ok")
            detached_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
            self.assertIsNone(detached_endpoint)
            self.assertFalse(old_aux_endpoint.attached)

            attached = runtime.execute(
                CapabilityCall(name="channel_attach", args={"target_id": "socket_aux"})
            )

            self.assertEqual(attached.status, "ok")
            attached_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
            self.assertIsNotNone(attached_endpoint)
            self.assertIsNot(attached_endpoint, old_aux_endpoint)
            self.assertTrue(attached_endpoint.attached)
            self.assertIn("pal.channel.endpoints.socket_endpoint", attached.structured["reload_modules"])
        finally:
            for name in list(sys.modules):
                if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
                    sys.modules.pop(name, None)
            sys.modules.update(original_modules)

    def test_core_lifecycle_owner_can_reload_channel_endpoint_provider(self) -> None:
        self.wizard.seed_defaults(self.registration)
        ChannelEndpointRepository().upsert(
            endpoint_id="socket_aux",
            channel_kind="socket",
            binding_key=str(self.registration.runtime.runtime_root / "aux.sock"),
            enabled=True,
            supports_typing=False,
            supports_receipt_marker=False,
            binding_metadata={},
            send_policy_blob={},
        )
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        endpoint_module_id = "channel.endpoint:socket_aux"
        old_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
        self.assertIsNotNone(old_endpoint)
        self.assertIsNotNone(handle.core.context.lifecycle_owner_registry.resolve(endpoint_module_id))
        prefixes = ("pal.channel.factory", "pal.channel.endpoints.socket_endpoint")
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
        }
        probe_name = "pal.channel.endpoints.socket_endpoint.__pal_hot_reload_probe__"
        try:
            detached = handle.core.detach_module(endpoint_module_id)
            self.assertEqual(detached, "ok")
            detached_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
            self.assertIsNone(detached_endpoint)
            self.assertFalse(old_endpoint.attached)

            reattached = handle.core.reattach_module(endpoint_module_id)

            self.assertEqual(reattached, "ok")
            new_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
            self.assertIsNotNone(new_endpoint)
            self.assertIsNot(new_endpoint, old_endpoint)
            self.assertTrue(new_endpoint.attached)
            self.assertIn("channel", handle.core.context.module_registry.modules)
        finally:
            for name in list(sys.modules):
                if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
                    sys.modules.pop(name, None)
            sys.modules.update(original_modules)

    def test_core_lifecycle_owner_cannot_detach_recovery_socket_endpoint(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        endpoint_module_id = "channel.endpoint:socket_default"
        old_endpoint = handle.channel_runtime.get_endpoint("socket_default")
        self.assertIsNotNone(old_endpoint)

        detached = handle.core.detach_module(endpoint_module_id)

        self.assertEqual(detached, "invalid")
        self.assertIs(handle.channel_runtime.get_endpoint("socket_default"), old_endpoint)
        self.assertTrue(old_endpoint.attached)

    def test_compose_runtime_registers_telegram_endpoint_via_runtime_provider(self) -> None:
        self.wizard.seed_defaults(self.registration)
        ChannelEndpointRepository().upsert(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat:123",
            enabled=True,
            supports_typing=True,
            supports_receipt_marker=True,
            binding_metadata={"bot_token": "secret-bot-token"},
            send_policy_blob={"max_message_chars": 4096},
        )
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        endpoint = handle.channel_runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.__class__.__name__, "TelegramChannelEndpoint")
        self.assertEqual(endpoint.endpoint.binding_key, "chat:123")
        manager = handle.core.context.require_port("channel:provider_manager")
        provider = manager.provider_for_endpoint_type("telegram")
        self.assertIsNotNone(provider)
        self.assertEqual(
            {item["provider_id"]: item for item in manager.list_providers()}["telegram"]["source"],
            "runtime_root",
        )
        self.assertIsNone(handle.core.context.module_registry.get("telegram_channel"))

    def test_channel_provider_rescan_uses_manager_provider_registry(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        result = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="channel_provider_rescan")
        )

        self.assertEqual(result.status, "ok")
        self.assertIn("socket", result.structured["providers_after"])
        self.assertIn("telegram", result.structured["providers_after"])
        self.assertIn("websocket_bridge", result.structured["providers_after"])
        self.assertEqual(result.structured["endpoint_type_map"]["socket"], "socket")
        self.assertEqual(result.structured["endpoint_type_map"]["telegram"], "telegram")
        self.assertEqual(
            result.structured["endpoint_type_map"]["websocket_bridge"],
            "websocket_bridge",
        )

    def test_compose_runtime_loads_runtime_root_channel_provider(self) -> None:
        self.wizard.seed_defaults(self.registration)
        self._write_demo_runtime_channel_provider()
        ChannelEndpointRepository().upsert(
            endpoint_id="demo_runtime_main",
            channel_kind="demo_runtime",
            binding_key="demo:1",
            enabled=True,
        )

        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        manager = handle.core.context.require_port("channel:provider_manager")
        endpoint = handle.channel_runtime.get_endpoint("demo_runtime_main")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.__class__.__name__, "DemoRuntimeEndpoint")
        provider = manager.provider_for_endpoint_type("demo_runtime")
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertEqual(provider.provider_id, "demo_runtime")
        provider_rows = {item["provider_id"]: item for item in manager.list_providers()}
        self.assertEqual(provider_rows["demo_runtime"]["source"], "runtime_root")

        health = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="channel_endpoint_health__demo_runtime_main",
                args={"target_id": "demo_runtime_main"},
            )
        )

        self.assertEqual(health.status, "ok")
        self.assertEqual(health.structured["provider_id"], "demo_runtime")
        self.assertEqual(health.structured["source"], "runtime_root_provider")
        self.assertTrue(health.structured["healthy"])

    def test_channel_provider_rescan_loads_new_runtime_root_provider(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        self.assertIsNone(handle.channel_runtime.get_endpoint("demo_runtime_main"))

        self._write_demo_runtime_channel_provider()
        ChannelEndpointRepository().upsert(
            endpoint_id="demo_runtime_main",
            channel_kind="demo_runtime",
            binding_key="demo:1",
            enabled=True,
        )

        result = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="channel_provider_rescan",
                args={"attach_enabled_endpoints": True},
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertIn("demo_runtime", result.structured["runtime_provider_ids"])
        self.assertIn("demo_runtime_main", result.structured["hydrated_endpoint_ids"])
        self.assertIn("channel_endpoint_health__demo_runtime_main", result.structured["republished_capability_names"])
        endpoint = handle.channel_runtime.get_endpoint("demo_runtime_main")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.__class__.__name__, "DemoRuntimeEndpoint")

        health = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="channel_endpoint_health__demo_runtime_main",
                args={"target_id": "demo_runtime_main"},
            )
        )

        self.assertEqual(health.status, "ok")
        self.assertEqual(health.structured["source"], "runtime_root_provider")

    def test_channel_provider_reload_preserves_runtime_telegram_auth_state(self) -> None:
        repository = ChannelEndpointRepository()
        repository.upsert(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat:123",
            binding_metadata={},
        )
        runtime = ChannelRuntime()
        old_endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:123"),
            runtime_root=self.runtime_root,
            bot_token="runtime-only-token",
        )
        old_endpoint.paired = True
        old_endpoint._authorized = True
        runtime.register_endpoint(old_endpoint)
        provider = ChannelIntrospectionProvider(
            runtime=runtime,
            repository=repository,
            runtime_root=self.runtime_root,
        )
        provider.provider_manager.register_provider(
            FactoryChannelProvider(
                provider_id="telegram",
                endpoint_types=("telegram",),
                factory=TelegramChannelEndpointFactory(),
                reload_modules=(),
            )
        )

        result = provider._restart_endpoint("telegram_main")

        self.assertEqual(result.status, "ok")
        new_endpoint = runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(new_endpoint)
        assert new_endpoint is not None
        self.assertIsNot(new_endpoint, old_endpoint)
        self.assertEqual(getattr(new_endpoint, "bot_token", ""), "runtime-only-token")
        self.assertTrue(getattr(new_endpoint, "_authorized", False))
        self.assertTrue(new_endpoint.paired)

    def test_llm_capabilities_are_read_only_and_do_not_expose_credentials(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        llm_list = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="llm_list")
        )
        llm_active = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="llm_active")
        )
        llm_think_level = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="llm_think_level")
        )
        llm_show = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="llm_show", args={"model_id": "stub-model"})
        )

        self.assertEqual(llm_list.status, "ok")
        self.assertEqual(llm_active.status, "ok")
        self.assertEqual(llm_think_level.status, "ok")
        self.assertEqual(llm_show.status, "ok")
        self.assertEqual(llm_list.structured["items"][0]["model_id"], "stub-model")
        self.assertEqual(llm_active.structured["model_id"], "stub-model")
        self.assertEqual(llm_active.structured["active_endpoint_id"], "stub_llm_default")
        self.assertNotIn("credential_ref", llm_show.structured)
        self.assertNotIn("base_url", llm_show.structured)
        self.assertEqual(llm_show.structured["model_id"], "stub-model")
        self.assertEqual(llm_show.structured["endpoint_id"], "stub_llm_default")
        self.assertIsNone(llm_think_level.structured["effective_think_level"])
        self.assertEqual(llm_think_level.structured["available_levels"], ())

    def test_compose_runtime_consumes_wizard_owned_database(self) -> None:
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        self.assertEqual(handle.database.db_path, self.registration.runtime.db_path)
        self.assertEqual(handle.registration.display_name, "PalV2 Test")

    def test_web_search_capability_falls_back_and_auth_material_is_sanitized(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        service = handle.core.context.module_registry.require("web_search").ports["web_search"]

        auth_result = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="web_search_provider_set_auth_material__brave_search_default",
                args={
                    "target_id": "brave_search_default",
                    "material": {"api_key": "brave-secret"},
                },
            )
        )

        self.assertEqual(auth_result.status, "ok")
        self.assertTrue(auth_result.structured["api_key_present"])
        self.assertEqual(auth_result.structured["accepted_keys"], ["api_key"])
        self.assertNotIn("api_key", auth_result.structured)
        self.assertEqual(
            WebSearchProviderRepository().get("brave_search_default").auth_material_blob["api_key"],
            "brave-secret",
        )

        class RaisingSearchProvider:
            provider_kind = "brave_search"

            def search(self, record, query):
                _ = record
                _ = query
                raise RuntimeError("brave unavailable")

        class StaticSearchProvider:
            provider_kind = "duckduckgo_search"

            def search(self, record, query):
                _ = query
                return [
                    WebSearchItem(
                        title="Pal runtime docs",
                        url="https://example.com/pal",
                        snippet="Pal documentation result",
                        provider_id=record.provider_id,
                        provider_kind=record.provider_kind,
                        rank=1,
                    )
                ]

        service.providers = {
            "brave_search": RaisingSearchProvider(),
            "duckduckgo_search": StaticSearchProvider(),
        }

        result = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_web_search",
                args={"query": "pal runtime docs", "limit": 3},
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["fallback_used"])
        self.assertEqual(result.structured["configured_provider_id"], "brave_search_default")
        self.assertEqual(result.structured["effective_provider_id"], "duckduckgo_search_default")
        self.assertEqual(result.structured["items"][0]["title"], "Pal runtime docs")

    def test_web_fetch_health_does_not_start_browser_service_and_disable_stops_manager(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        service = handle.core.context.module_registry.require("web_fetch").ports["web_fetch"]

        health = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="web_fetch_provider_health__playwright_fetch_default",
                args={"target_id": "playwright_fetch_default"},
            )
        )

        self.assertEqual(health.status, "ok")
        self.assertIsNone(service.browser_manager.process)
        self.assertFalse(health.structured["service_running"])

        class FakeBrowserManager:
            def __init__(self) -> None:
                self.stop_calls = 0

            def stop_sync(self) -> None:
                self.stop_calls += 1

            async def shutdown_async(self) -> None:
                return None

            def health(self, *, settings=None):
                _ = settings
                return {"healthy": True, "service_running": False, "reason": "idle"}

        fake_manager = FakeBrowserManager()
        service.browser_manager = fake_manager

        disabled = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="web_fetch_provider_disable__playwright_fetch_default",
                args={"target_id": "playwright_fetch_default"},
            )
        )

        self.assertEqual(disabled.status, "ok")
        self.assertEqual(fake_manager.stop_calls, 1)

    def test_web_fetch_capability_falls_back_to_plain_http_and_runtime_stop_runs_shutdown_hook(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        service = handle.core.context.module_registry.require("web_fetch").ports["web_fetch"]
        seen_requests: list[object] = []

        class RaisingFetchProvider:
            provider_kind = "playwright_fetch"

            def read(self, record, request):
                _ = record
                _ = request
                raise RuntimeError("browser unavailable")

        class StaticFetchProvider:
            provider_kind = "plain_http_fetch"

            def read(self, record, request):
                _ = record
                seen_requests.append(request)
                return {
                    "requested_url": request.url,
                    "final_url": "https://example.com/final",
                    "title": "Example Domain",
                    "text": "Example body text",
                    "raw_content": "<html><body>Example body text</body></html>",
                    "raw_content_truncated": False,
                    "status_code": 200,
                    "content_type": "text/html; charset=utf-8",
                    "content_length": 1234,
                    "text_truncated": True,
                    "links": [{"href": "https://example.com/about", "text": "About", "rel": ""}],
                    "metadata": {"description": "Example metadata"},
                    "response_headers": {"content-type": "text/html; charset=utf-8"},
                }

        service.providers = {
            "playwright_fetch": RaisingFetchProvider(),
            "plain_http_fetch": StaticFetchProvider(),
        }

        result = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_web_read",
                args={"url": "https://example.com"},
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["fallback_used"])
        self.assertEqual(result.structured["configured_provider_id"], "playwright_fetch_default")
        self.assertEqual(result.structured["effective_provider_id"], "plain_http_fetch_default")
        self.assertEqual(result.structured["fetch_mode"], "http")
        self.assertEqual(result.structured["final_url"], "https://example.com/final")
        self.assertEqual(result.structured["status_code"], 200)
        self.assertEqual(result.structured["content_type"], "text/html; charset=utf-8")
        self.assertTrue(result.structured["text_truncated"])
        self.assertTrue(result.structured["raw_content_available"])
        self.assertFalse(result.structured["raw_content_truncated"])
        self.assertEqual(result.structured["links"][0]["href"], "https://example.com/about")
        self.assertEqual(result.structured["metadata"]["description"], "Example metadata")
        self.assertIn("Chrome/", result.structured["user_agent"])
        self.assertEqual(seen_requests[0].user_agent, DEFAULT_WEB_FETCH_USER_AGENT)

        class ShutdownTrackingManager:
            def __init__(self) -> None:
                self.shutdown_calls = 0

            def stop_sync(self) -> None:
                return None

            async def shutdown_async(self) -> None:
                self.shutdown_calls += 1

            def health(self, *, settings=None):
                _ = settings
                return {"healthy": True, "service_running": False, "reason": "idle"}

        tracking_manager = ShutdownTrackingManager()
        service.browser_manager = tracking_manager

        asyncio.run(handle.stop_async())

        self.assertEqual(tracking_manager.shutdown_calls, 1)

    def test_plain_http_fetch_uses_chrome_user_agent_and_preserves_page_metadata(self) -> None:
        captured = {}

        class FakeHeaders:
            def get(self, key, default=None):
                values = {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Length": "321",
                }
                return values.get(key, default)

            def items(self):
                return {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Length": "321",
                    "Set-Cookie": "secret=ignored",
                }.items()

        class FakeResponse:
            headers = FakeHeaders()
            status = 203

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                _ = (exc_type, exc, tb)
                return False

            def geturl(self):
                return "https://example.test/final"

            def read(self):
                return (
                    b"<html lang='en'><head><title>Example</title>"
                    b"<meta name='description' content='A useful page'>"
                    b"<link rel='canonical' href='https://example.test/canonical'>"
                    b"</head><body><a href='/docs' rel='help'>Docs</a>"
                    b"<p>Hello world</p></body></html>"
                )

        def fake_urlopen(request, timeout):
            captured["user_agent"] = request.get_header("User-agent")
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("pal.web_fetch.browser_service.urlopen", fake_urlopen):
            document = plain_http_fetch("https://example.test", timeout_ms=2500, max_chars=20, max_raw_chars=30)

        self.assertIn("Chrome/", captured["user_agent"])
        self.assertEqual(captured["timeout"], 2.5)
        self.assertEqual(document.status_code, 203)
        self.assertEqual(document.final_url, "https://example.test/final")
        self.assertEqual(document.title, "Example")
        self.assertTrue(document.text_truncated)
        self.assertTrue(document.raw_content.startswith("<html"))
        self.assertTrue(document.raw_content_truncated)
        self.assertEqual(document.links[0].href, "/docs")
        self.assertEqual(document.links[0].text, "Docs")
        self.assertEqual(document.metadata["description"], "A useful page")
        self.assertEqual(document.metadata["canonical_url"], "https://example.test/canonical")
        self.assertIn("content-type", document.response_headers or {})
        self.assertNotIn("Set-Cookie", document.response_headers or {})


class PalV2SocketEndpointUnitTests(unittest.TestCase):
    def test_unstarted_socket_endpoint_stop_does_not_unlink_existing_socket_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_socket_owner_test_") as root:
            socket_path = Path(root) / "pal.sock"
            socket_path.write_text("owned by another runtime", encoding="utf-8")
            endpoint = SocketChannelEndpoint(
                endpoint=EndpointConfig(
                    endpoint_id="socket_default",
                    channel_kind="socket",
                    binding_key=str(socket_path),
                )
            )

            asyncio.run(endpoint.stop_async())

            self.assertTrue(socket_path.exists())
            self.assertEqual(socket_path.read_text(encoding="utf-8"), "owned by another runtime")

    def test_streamed_text_final_reply_state_does_not_mutate_response_handle(self) -> None:
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key="pal.sock",
            )
        )
        outbound = _OutboundQueue()
        endpoint.sessions["session-1"] = type("Session", (), {"outbound": outbound, "closed": False})()
        response_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={"session_id": "session-1", "request_id": "req-1"},
        )

        endpoint.send_stream_event(
            response_handle,
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="pong"),
        )
        endpoint.queue_reply("pong", response_handle=response_handle)

        self.assertEqual(outbound.items, [{"type": "text_delta", "request_id": "req-1", "text": "pong"}])
        self.assertFalse(endpoint.outbox)
        self.assertNotIn(id(response_handle), endpoint._streamed_text_handles)

    def test_streamed_text_final_reply_suppresses_equivalent_response_handle_copy(self) -> None:
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key="pal.sock",
            )
        )
        outbound = _OutboundQueue()
        endpoint.sessions["session-1"] = type("Session", (), {"outbound": outbound, "closed": False})()
        stream_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={"session_id": "session-1", "request_id": "req-1"},
        )
        final_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={"session_id": "session-1", "request_id": "req-1"},
        )

        endpoint.send_stream_event(
            stream_handle,
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="pong"),
        )
        endpoint.queue_reply("pong", response_handle=final_handle)

        self.assertEqual(outbound.items, [{"type": "text_delta", "request_id": "req-1", "text": "pong"}])
        self.assertFalse(endpoint.outbox)
        self.assertFalse(endpoint._streamed_text_keys)
        self.assertFalse(endpoint._stream_handle_ids_by_key)

    def test_queued_streamed_text_suppresses_final_reply_before_flush(self) -> None:
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key="pal.sock",
            )
        )
        outbound = _OutboundQueue()
        endpoint.sessions["session-1"] = type("Session", (), {"outbound": outbound, "closed": False})()
        stream_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={"session_id": "session-1", "request_id": "req-1"},
        )
        final_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={"session_id": "session-1", "request_id": "req-1"},
        )

        endpoint.queue_stream_event(
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="pong"),
            response_handle=stream_handle,
        )
        endpoint.queue_reply("pong", response_handle=final_handle)

        self.assertEqual(len(endpoint.stream_outbox), 1)
        self.assertFalse(endpoint.outbox)

    def test_telegram_streamed_text_keeps_final_reply_for_batched_delivery(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:42",
            )
        )
        response_handle = ResponseHandle(endpoint_id="telegram_main", reply_target={"chat_id": "42"})

        endpoint.send_stream_event(
            response_handle,
            NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="pong"),
        )
        endpoint.queue_reply("pong", response_handle=response_handle)

        self.assertEqual(len(endpoint.outbox), 1)
        self.assertEqual(endpoint.outbox[0].text, "pong")


class PalV2SocketEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not hasattr(asyncio, "start_unix_server") or not hasattr(asyncio, "open_unix_connection"):
            self.skipTest("Unix socket asyncio APIs are unavailable on this platform")
        if not _unix_socket_bind_available():
            self.skipTest("Unix socket binding is unavailable in this test sandbox")
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_socket_test_"))
        self.socket_path = self.runtime_root / "pal.sock"
        self.endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key=str(self.socket_path),
            )
        )

    async def asyncTearDown(self) -> None:
        try:
            await self.endpoint.stop_async()
        except Exception:
            pass
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    async def _poll_until_envelopes(self, expected_count: int = 1, *, timeout_seconds: float = 1.0):
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        envelopes = []
        while True:
            envelopes = self.endpoint.poll()
            if len(envelopes) >= expected_count:
                return envelopes
            if asyncio.get_running_loop().time() >= deadline:
                return envelopes
            await asyncio.sleep(0.01)

    async def test_socket_endpoint_unlinks_stale_socket_before_bind(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.write_text("stale")

        await self.endpoint.start_async()

        self.assertIsNotNone(self.endpoint.server)
        self.assertTrue(self.socket_path.exists())

    async def test_socket_endpoint_accepts_message_and_streams_reply(self) -> None:
        await self.endpoint.start_async()
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(
                pack_socket_message(
                    {
                        "type": "user_message",
                        "request_id": "req-1",
                        "text": "ping",
                    }
                )
            )
            await writer.drain()

            envelopes = await self._poll_until_envelopes()
            self.assertEqual(len(envelopes), 1)
            envelope = envelopes[0]
            self.assertEqual(envelope.event.payload["text"], "ping")

            self.endpoint.queue_reply("pong", response_handle=envelope.response_handle)
            emitted = self.endpoint.flush_outbox()

            self.assertEqual([event.event_kind for event in emitted], ["reply.delivered"])
            first = await read_socket_message(reader)
            second = await read_socket_message(reader)
            self.assertEqual(first["type"], "text_delta")
            self.assertEqual(first["text"], "pong")
            self.assertEqual(second["type"], "done")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_socket_endpoint_suppresses_final_reply_after_text_stream(self) -> None:
        await self.endpoint.start_async()
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(
                pack_socket_message(
                    {
                        "type": "user_message",
                        "request_id": "req-stream",
                        "text": "ping",
                    }
                )
            )
            await writer.drain()
            envelopes = await self._poll_until_envelopes()
            self.assertEqual(len(envelopes), 1)
            envelope = envelopes[0]

            self.endpoint.queue_stream_event(
                NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="po"),
                response_handle=envelope.response_handle,
            )
            self.endpoint.queue_stream_event(
                NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="ng"),
                response_handle=envelope.response_handle,
            )
            self.endpoint.queue_stream_event(
                NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.DONE, finish_reason="stop"),
                response_handle=envelope.response_handle,
            )
            self.assertEqual(self.endpoint.flush_stream_outbox(), [])

            first = await read_socket_message(reader)
            second = await read_socket_message(reader)
            third = await read_socket_message(reader)
            self.assertEqual(first["type"], "text_delta")
            self.assertEqual(first["text"], "po")
            self.assertEqual(second["type"], "text_delta")
            self.assertEqual(second["text"], "ng")
            self.assertEqual(third["type"], "llm_done")

            self.endpoint.queue_reply("pong", response_handle=envelope.response_handle)
            self.assertEqual(self.endpoint.flush_outbox(), [])
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(read_socket_message(reader), timeout=0.05)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_socket_endpoint_disconnect_marks_delivery_failure_without_crashing(self) -> None:
        await self.endpoint.start_async()
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(
                pack_socket_message(
                    {
                        "type": "user_message",
                        "request_id": "req-2",
                        "text": "ping",
                    }
                )
            )
            await writer.drain()
            envelopes = await self._poll_until_envelopes()
            self.assertEqual(len(envelopes), 1)
            envelope = envelopes[0]
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)

        self.endpoint.queue_reply("pong", response_handle=envelope.response_handle)
        emitted = self.endpoint.flush_outbox()

        self.assertEqual([event.event_kind for event in emitted], ["reply.failed"])
        self.assertEqual(emitted[0].payload.get("permanent"), True)
        self.assertEqual(emitted[0].payload.get("attempts"), 1)
        self.assertFalse(self.endpoint.outbox)


class _FakeTelegramFile:
    def __init__(self, *, content: bytes, file_path: str = "telegram/file.bin") -> None:
        self._content = content
        self.file_path = file_path

    async def download_as_bytearray(self):
        return bytearray(self._content)


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.actions: list[tuple[str, dict[str, object]]] = []
        self.files: dict[str, _FakeTelegramFile] = {}
        self.message_delays: dict[str, float] = {}
        self._next_message_id = 1000

    async def set_message_reaction(self, **kwargs):
        self.actions.append(("reaction", dict(kwargs)))

    async def send_chat_action(self, **kwargs):
        self.actions.append(("typing", dict(kwargs)))

    async def send_message(self, **kwargs):
        text = str(kwargs.get("text") or "")
        delay = self.message_delays.get(text, self.message_delays.get(text.strip(), 0.0))
        if delay > 0:
            await asyncio.sleep(delay)
        self.actions.append(("message", dict(kwargs)))
        message_id = self._next_message_id
        self._next_message_id += 1

        class _SentMessage:
            def __init__(self, message_id: int) -> None:
                self.message_id = message_id

        return _SentMessage(message_id)

    async def edit_message_text(self, **kwargs):
        self.actions.append(("edit_message_text", dict(kwargs)))

    async def get_file(self, file_id: str):
        return self.files[file_id]

    async def set_my_commands(self, commands, **kwargs):
        normalized = [
            {
                "command": str(getattr(item, "command", "")),
                "description": str(getattr(item, "description", "")),
            }
            for item in list(commands or [])
        ]
        payload = dict(kwargs)
        payload["commands"] = normalized
        self.actions.append(("commands", payload))

    async def set_chat_menu_button(self, **kwargs):
        self.actions.append(("menu_button", dict(kwargs)))


class _FakeTelegramUpdater:
    def __init__(self) -> None:
        self.running = False
        self.start_kwargs: list[dict[str, object]] = []
        self.stop_count = 0

    async def start_polling(self, **kwargs):
        self.start_kwargs.append(dict(kwargs))
        self.running = True

    async def stop(self):
        self.stop_count += 1
        self.running = False


class _HangingTelegramUpdater(_FakeTelegramUpdater):
    async def stop(self):
        self.stop_count += 1
        await asyncio.Event().wait()


class _FakeTelegramApp:
    def __init__(self, bot: _FakeTelegramBot, *, fail_initialize: bool = False) -> None:
        self.bot = bot
        self.updater = _FakeTelegramUpdater()
        self.running = False
        self.fail_initialize = fail_initialize

    def add_handler(self, handler) -> None:
        _ = handler

    def add_error_handler(self, handler) -> None:
        _ = handler

    async def initialize(self):
        if self.fail_initialize:
            raise RuntimeError("init failed")
        return None

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        return None


class _HangingStopTelegramApp(_FakeTelegramApp):
    def __init__(self, bot: _FakeTelegramBot) -> None:
        super().__init__(bot)
        self.updater = _HangingTelegramUpdater()


class _FakeTelegramUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeTelegramChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeTelegramDocument:
    def __init__(self, *, file_id: str, file_name: str, mime_type: str) -> None:
        self.file_id = file_id
        self.file_name = file_name
        self.mime_type = mime_type


class _FakeTelegramMessage:
    def __init__(
        self,
        *,
        chat_id: int,
        user_id: int,
        message_id: int,
        text: str | None = None,
        caption: str | None = None,
        message_thread_id: int | None = None,
        document: _FakeTelegramDocument | None = None,
    ) -> None:
        self.chat = _FakeTelegramChat(chat_id)
        self.from_user = _FakeTelegramUser(user_id)
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.message_thread_id = message_thread_id
        self.document = document
        self.photo = []
        self.audio = None


class _FakeTelegramUpdate:
    def __init__(self, *, update_id: int, message: _FakeTelegramMessage) -> None:
        self.update_id = update_id
        self.effective_message = message


class PalV2TelegramEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_telegram_test_"))
        self.endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={"max_message_chars": 32},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        self.fake_bot = _FakeTelegramBot()
        self.fake_app = _FakeTelegramApp(self.fake_bot)
        self.endpoint._build_application = lambda: self.fake_app  # type: ignore[method-assign]
        await self.endpoint.start_async()

    async def asyncTearDown(self) -> None:
        try:
            await self.endpoint.stop_async()
        except Exception:
            pass
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    async def test_telegram_endpoint_filters_non_matching_binding_user(self) -> None:
        update = _FakeTelegramUpdate(
            update_id=1,
            message=_FakeTelegramMessage(chat_id=100, user_id=7, message_id=9, text="ignored"),
        )

        await self.endpoint._on_update(update, None)

        self.assertEqual(self.endpoint.poll(), [])

    async def test_telegram_endpoint_uses_separate_get_updates_connection_pool(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_pool",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
            poll_timeout_seconds=30,
        )

        app = endpoint._build_application()
        get_updates_request, bot_request = app.bot._request

        self.assertIsNot(get_updates_request, bot_request)
        self.assertEqual(get_updates_request._client_kwargs["limits"].max_connections, 4)
        self.assertEqual(get_updates_request._client_kwargs["timeout"].read, 40.0)
        self.assertEqual(get_updates_request._client_kwargs["timeout"].pool, 30)
        self.assertEqual(bot_request._client_kwargs["limits"].max_connections, 32)

    async def test_telegram_endpoint_accepts_matching_binding_and_queues_receipt_only(self) -> None:
        update = _FakeTelegramUpdate(
            update_id=2,
            message=_FakeTelegramMessage(chat_id=100, user_id=42, message_id=10, text="hello"),
        )

        await self.endpoint._on_update(update, None)

        await asyncio.sleep(0.05)
        self.assertFalse(any(kind == "reaction" for kind, _ in self.fake_bot.actions))
        self.assertFalse(any(kind == "typing" for kind, _ in self.fake_bot.actions))
        self.assertEqual([item.kind for item in self.endpoint.status_outbox], ["receipt_marker"])

        envelopes = self.endpoint.poll()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].event.payload["text"], "hello")
        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)
        self.assertTrue(any(kind == "reaction" for kind, _ in self.fake_bot.actions))
        self.assertFalse(any(kind == "typing" for kind, _ in self.fake_bot.actions))

    async def test_telegram_endpoint_serializes_replies_for_same_thread(self) -> None:
        self.fake_bot.message_delays["first"] = 0.05
        handle = self.endpoint.build_response_handle(
            reply_target={"chat_id": "100", "message_id": "10", "thread_id": ""},
        )

        with patch.object(_telegram_module, "_telegram_markdown", side_effect=lambda text: (str(text), None)):
            self.endpoint.queue_reply("first", response_handle=handle)
            self.endpoint.queue_reply("second", response_handle=handle)
            self.endpoint.flush_outbox()
            pending = next(iter(self.endpoint._send_chains.values()))
            await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)

        sent_texts = [str(payload.get("text") or "").strip() for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertEqual(sent_texts[-2:], ["first", "second"])

    async def test_telegram_endpoint_downloads_document_without_reading_contents_into_payload(self) -> None:
        self.fake_bot.files["doc-1"] = _FakeTelegramFile(content=b"hello telegram", file_path="docs/file.txt")
        update = _FakeTelegramUpdate(
            update_id=3,
            message=_FakeTelegramMessage(
                chat_id=777,
                user_id=42,
                message_id=11,
                caption="with file",
                document=_FakeTelegramDocument(file_id="doc-1", file_name="report.txt", mime_type="text/plain"),
            ),
        )

        await self.endpoint._on_update(update, None)

        envelopes = self.endpoint.poll()
        self.assertEqual(len(envelopes), 1)
        attachments = envelopes[0].event.payload["attachments"]
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertTrue(str(attachment["local_cached_path"]).startswith(str(self.runtime_root / "artifacts" / "telegram" / "777")))
        self.assertFalse("hello telegram" in str(attachment))
        self.assertEqual(
            attachment["source_metadata"]["source_url"],
            "https://api.telegram.org/file/bottoken-123/docs/file.txt",
        )

    async def test_telegram_endpoint_preserves_absolute_file_url(self) -> None:
        absolute = "https://api.telegram.org/file/bottoken-123/photos/file_31.jpg"
        self.fake_bot.files["doc-absolute"] = _FakeTelegramFile(content=b"photo", file_path=absolute)
        update = _FakeTelegramUpdate(
            update_id=4,
            message=_FakeTelegramMessage(
                chat_id=777,
                user_id=42,
                message_id=12,
                document=_FakeTelegramDocument(file_id="doc-absolute", file_name="photo.jpg", mime_type="image/jpeg"),
            ),
        )

        await self.endpoint._on_update(update, None)

        envelopes = self.endpoint.poll()
        attachment = envelopes[0].event.payload["attachments"][0]
        self.assertEqual(attachment["source_metadata"]["source_url"], absolute)
        self.assertNotIn("/https://", attachment["source_metadata"]["source_url"].removeprefix("https://"))

    async def test_telegram_endpoint_registers_control_commands_and_menu(self) -> None:
        self.endpoint.queue_status(
            "control_catalog",
            payload={
                "commands": [
                    {"command": "control", "description": "Show the control panel and command help."},
                    {"command": "think", "description": "Show or update the think level for future turns."},
                    {"command": "model", "description": "Show or update the active LLM model for future turns."},
                    {
                        "command": "refresh_llm_endpoint",
                        "description": "Refresh LLM endpoint topology from the local database for future turns.",
                    },
                ]
            },
        )

        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        command_actions = [payload for kind, payload in self.fake_bot.actions if kind == "commands"]
        self.assertTrue(command_actions)
        published_commands = [item["command"] for item in command_actions[-1]["commands"]]
        self.assertIn("model", published_commands)
        self.assertIn("refresh_llm_endpoint", published_commands)
        self.assertTrue(any(kind == "menu_button" for kind, _ in self.fake_bot.actions))

    async def test_telegram_endpoint_interactive_update_and_resolve_reuse_same_message(self) -> None:
        spec = InteractionMessageSpec(
            interaction_id="ctl_panel_1",
            interaction_kind="control_panel",
            text="Pal Control Panel",
            buttons=((InteractionButtonSpec(label="Think", action_key="control.think.open"),),),
            expires_at=None,
        )
        self.endpoint.queue_status(
            "interactive_update",
            payload={"spec": spec},
            response_handle=self.endpoint.build_response_handle(reply_target={"chat_id": "42"}),
        )

        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        self.assertIn("ctl_panel_1", self.endpoint._interactive_messages)
        first_message = next(payload for kind, payload in self.fake_bot.actions if kind == "message")
        stored = self.endpoint._interactive_messages["ctl_panel_1"]
        self.assertEqual(stored["chat_id"], 42)
        self.assertEqual(first_message["text"], "Pal Control Panel")

        updated = InteractionMessageSpec(
            interaction_id="ctl_panel_1",
            interaction_kind="control_panel",
            text="Think level: balanced",
            buttons=((InteractionButtonSpec(label="Back", action_key="control.panel.back"),),),
            expires_at=None,
        )
        self.endpoint.queue_status(
            "interactive_update",
            payload={"spec": updated},
            response_handle=self.endpoint.build_response_handle(reply_target={"chat_id": "42"}),
        )
        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        self.assertTrue(any(kind == "edit_message_text" for kind, _ in self.fake_bot.actions))

        resolved = InteractionMessageSpec(
            interaction_id="ctl_panel_1",
            interaction_kind="control_panel",
            text="Done.",
            buttons=(),
            expires_at=None,
        )
        self.endpoint.queue_status(
            "interactive_resolve",
            payload={"spec": resolved},
            response_handle=self.endpoint.build_response_handle(reply_target={"chat_id": "42"}),
        )
        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        self.assertNotIn("ctl_panel_1", self.endpoint._interactive_messages)

    async def test_telegram_endpoint_segments_long_interaction_and_keeps_keyboard_on_last_message(self) -> None:
        spec = InteractionMessageSpec(
            interaction_id="review_long",
            interaction_kind="minion_v2_architecture_review",
            text=("architecture contract\n" * 600).strip(),
            buttons=(
                (
                    InteractionButtonSpec(
                        label="Accept",
                        action_key="control.action.dispatch",
                        action_args={"decision": "accept"},
                    ),
                ),
            ),
            expires_at=None,
        )
        self.endpoint.queue_status(
            "interactive_open",
            payload={"spec": spec},
            response_handle=self.endpoint.build_response_handle(
                reply_target={"chat_id": "42"},
            ),
        )

        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        messages = [
            payload
            for kind, payload in self.fake_bot.actions
            if kind == "message"
        ]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(str(item["text"])) <= 4096 for item in messages))
        self.assertNotIn("reply_markup", messages[0])
        self.assertIn("reply_markup", messages[-1])
        self.assertIn("review_long", self.endpoint._interactive_messages)

    async def test_telegram_endpoint_restores_durable_interaction_after_endpoint_restart(self) -> None:
        spec = InteractionMessageSpec(
            interaction_id="review_restart",
            interaction_kind="minion_v2_architecture_review",
            text="Review ready",
            buttons=(
                (
                    InteractionButtonSpec(
                        label="Accept",
                        action_key="control.action.dispatch",
                        action_args={
                            "action_kind": "minion_v2_human_decision",
                            "args": {"decision": "accept"},
                        },
                    ),
                ),
            ),
            expires_at=None,
        )
        self.endpoint.queue_status(
            "interactive_open",
            payload={"spec": spec},
            response_handle=self.endpoint.build_response_handle(
                reply_target={"chat_id": "42"},
            ),
        )
        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)
        self.assertTrue(
            (
                self.runtime_root
                / "data"
                / "channel"
                / "telegram_main"
                / "state.sqlite3"
            ).is_file()
        )

        restarted = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={"max_message_chars": 4096},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )

        class _CallbackQuery:
            data = "ix:review_restart:b0"
            message = _FakeTelegramMessage(
                chat_id=42,
                user_id=42,
                message_id=999,
                text="",
            )
            from_user = _FakeTelegramUser(42)
            id = "callback-restart"

            async def answer(self) -> None:
                return None

        class _Update:
            callback_query = _CallbackQuery()

        result = await restarted._interaction_result_from_update(_Update())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.interaction_id, "review_restart")
        self.assertEqual(result.action_key, "control.action.dispatch")
        self.assertEqual(
            result.action_args["args"]["decision"],
            "accept",
        )

    async def test_telegram_endpoint_interactive_update_falls_back_to_new_message_when_edit_fails(self) -> None:
        self.endpoint._interactive_messages["ctl_panel_stale"] = {
            "chat_id": 42,
            "message_id": 10,
            "interaction_kind": "control_panel",
            "expires_at_monotonic": None,
            "actions": {},
        }

        async def _raise_edit_message_text(**kwargs):
            _ = kwargs
            raise RuntimeError("message is not modified")

        self.fake_bot.edit_message_text = _raise_edit_message_text
        spec = InteractionMessageSpec(
            interaction_id="ctl_panel_stale",
            interaction_kind="control_panel",
            text="Pal Control Panel",
            buttons=((InteractionButtonSpec(label="Think", action_key="control.think.open"),),),
            expires_at=None,
        )
        self.endpoint.queue_status(
            "interactive_update",
            payload={"spec": spec},
            response_handle=self.endpoint.build_response_handle(reply_target={"chat_id": "42"}),
        )

        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        messages = [payload for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertTrue(messages)
        self.assertEqual(messages[-1]["text"], "Pal Control Panel")
        self.assertIn("ctl_panel_stale", self.endpoint._interactive_messages)
        self.assertNotEqual(self.endpoint._interactive_messages["ctl_panel_stale"]["message_id"], 10)

    async def test_telegram_endpoint_prunes_expired_interactions(self) -> None:
        self.endpoint._interactive_messages["expired"] = {
            "chat_id": 42,
            "message_id": 1000,
            "interaction_kind": "reset_confirm",
            "expires_at_monotonic": 1.0,
            "actions": {},
        }

        self.endpoint._prune_interactive_messages(now=2.0)

        self.assertNotIn("expired", self.endpoint._interactive_messages)

    async def test_telegram_endpoint_async_prune_expires_message_and_removes_keyboard(self) -> None:
        self.endpoint._interactive_messages["expired"] = {
            "chat_id": 42,
            "message_id": 1000,
            "interaction_kind": "reset_confirm",
            "expires_at_monotonic": 1.0,
            "actions": {},
        }

        await self.endpoint._prune_interactive_messages_async(now=2.0)

        self.assertNotIn("expired", self.endpoint._interactive_messages)
        edit = next(payload for kind, payload in self.fake_bot.actions if kind == "edit_message_text")
        self.assertEqual(edit["text"], "This reset request expired.")
        self.assertIsNone(edit["reply_markup"])

    async def test_telegram_endpoint_health_reflects_missing_token_without_starting_polling(self) -> None:
        other = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_missing",
                channel_kind="telegram",
                binding_key="chat:9",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="",
        )
        await other.start_async()
        try:
            health = other.inspect_health()
            self.assertFalse(health["healthy"])
            self.assertEqual(health["reason"], "token_missing")
            self.assertFalse(health["polling_running"])
        finally:
            await other.stop_async()

    async def test_telegram_endpoint_reconnects_after_startup_failure(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_retry",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        endpoint._reconnect_delays = (0.01, 0.01)
        apps = [
            _FakeTelegramApp(self.fake_bot, fail_initialize=True),
            _FakeTelegramApp(self.fake_bot),
        ]

        def build_app():
            return apps.pop(0)

        endpoint._build_application = build_app  # type: ignore[method-assign]
        await endpoint.start_async()
        try:
            await asyncio.sleep(0.05)
            health = endpoint.inspect_health()
            self.assertTrue(health["healthy"])
            self.assertTrue(health["polling_running"])
            self.assertEqual(health["reconnect_attempts"], 0)
            self.assertEqual(health["last_poll_error"], "")
        finally:
            await endpoint.stop_async()

    async def test_telegram_endpoint_reconnects_after_persistent_poll_error_without_dropping_updates(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_poll_retry",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        endpoint._reconnect_delays = (0.01, 0.01)
        endpoint._poll_error_stale_threshold_seconds = 0.01
        endpoint._poll_monitor_interval_seconds = 0.01
        apps = [
            _FakeTelegramApp(self.fake_bot),
            _FakeTelegramApp(self.fake_bot),
        ]

        def build_app():
            return apps.pop(0)

        endpoint._build_application = build_app  # type: ignore[method-assign]
        await endpoint.start_async()
        first_app = endpoint.application
        self.assertIsNotNone(first_app)
        self.assertEqual(first_app.updater.start_kwargs[-1]["drop_pending_updates"], True)

        class _ErrorContext:
            error = RuntimeError("telegram network down")

        await endpoint._on_error(None, _ErrorContext())

        try:
            for _ in range(20):
                await asyncio.sleep(0.01)
                if endpoint.application is not first_app and endpoint.inspect_health()["polling_running"]:
                    break

            self.assertIsNot(endpoint.application, first_app)
            self.assertGreaterEqual(first_app.updater.stop_count, 1)
            self.assertTrue(endpoint.inspect_health()["polling_running"])
            self.assertEqual(endpoint.application.updater.start_kwargs[-1]["drop_pending_updates"], False)
        finally:
            await endpoint.stop_async()

    async def test_telegram_endpoint_observes_get_updates_timeout_as_poll_error(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_observed_timeout",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        app = endpoint._build_application()
        get_updates_request, bot_request = app.bot._request

        import httpx
        from telegram.error import TimedOut

        with patch.object(
            get_updates_request._client,
            "request",
            new=AsyncMock(side_effect=httpx.ReadTimeout("read timed out")),
        ):
            with self.assertRaises(TimedOut):
                await get_updates_request.do_request("https://api.telegram.org/bottoken-123/getUpdates", "POST")

        try:
            health = endpoint.inspect_health()
            self.assertEqual(health["reason"], "polling_error")
            self.assertIn("TimedOut", health["last_poll_error"])
            self.assertEqual(endpoint._get_updates_in_flight_started_at, 0.0)
        finally:
            await get_updates_request.shutdown()
            await bot_request.shutdown()

    async def test_telegram_endpoint_reconnects_after_observed_get_updates_timeout(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_timeout_retry",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        endpoint._reconnect_delays = (0.01, 0.01)
        endpoint._poll_error_stale_threshold_seconds = 0.01
        endpoint._poll_monitor_interval_seconds = 0.01
        apps = [
            _FakeTelegramApp(self.fake_bot),
            _FakeTelegramApp(self.fake_bot),
        ]

        def build_app():
            return apps.pop(0)

        endpoint._build_application = build_app  # type: ignore[method-assign]
        await endpoint.start_async()
        first_app = endpoint.application
        self.assertIsNotNone(first_app)

        from telegram.error import TimedOut

        endpoint._record_get_updates_failure(TimedOut("Timed out"))

        try:
            for _ in range(30):
                await asyncio.sleep(0.01)
                if endpoint.application is not first_app and endpoint.inspect_health()["polling_running"]:
                    break

            self.assertIsNot(endpoint.application, first_app)
            self.assertGreaterEqual(first_app.updater.stop_count, 1)
            health = endpoint.inspect_health()
            self.assertTrue(health["polling_running"])
            self.assertEqual(health["last_poll_error"], "")
            self.assertEqual(endpoint.application.updater.start_kwargs[-1]["drop_pending_updates"], False)
        finally:
            await endpoint.stop_async()

    async def test_telegram_endpoint_reconnects_even_when_old_updater_stop_hangs(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_hanging_shutdown_retry",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        endpoint._reconnect_delays = (0.01, 0.01)
        endpoint._poll_error_stale_threshold_seconds = 0.01
        endpoint._poll_monitor_interval_seconds = 0.01
        endpoint._application_shutdown_timeout_seconds = 0.01
        apps = [
            _HangingStopTelegramApp(self.fake_bot),
            _FakeTelegramApp(self.fake_bot),
        ]

        def build_app():
            return apps.pop(0)

        endpoint._build_application = build_app  # type: ignore[method-assign]
        await endpoint.start_async()
        first_app = endpoint.application
        self.assertIsNotNone(first_app)

        endpoint._record_get_updates_failure(RuntimeError("TimedOut"))

        try:
            for _ in range(30):
                await asyncio.sleep(0.01)
                if endpoint.application is not first_app and endpoint.inspect_health()["polling_running"]:
                    break

            self.assertIsNot(endpoint.application, first_app)
            self.assertEqual(first_app.updater.stop_count, 1)
            self.assertTrue(endpoint.inspect_health()["polling_running"])
        finally:
            await endpoint.stop_async()

    async def test_telegram_endpoint_polling_error_callback_updates_health(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_poll_error",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token-123",
        )
        app = _FakeTelegramApp(self.fake_bot)
        endpoint._build_application = lambda: app  # type: ignore[method-assign]
        await endpoint.start_async()
        try:
            callback = app.updater.start_kwargs[-1]["error_callback"]
            self.assertIs(callback.__self__, endpoint)
            self.assertIs(callback.__func__, endpoint._on_polling_error.__func__)

            callback(RuntimeError("telegram pool exhausted"))

            health = endpoint.inspect_health()
            self.assertFalse(health["healthy"])
            self.assertEqual(health["reason"], "polling_error")
            self.assertEqual(health["last_poll_error"], "telegram pool exhausted")
        finally:
            await endpoint.stop_async()
