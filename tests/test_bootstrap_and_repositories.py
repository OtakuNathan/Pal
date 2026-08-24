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
from pal.channel import ChannelEndpointRepository, ChannelProviderContext, ChannelRuntime, FactoryChannelProvider
from pal.channel.contracts import ChannelMessage, EndpointConfig, ResponseHandle
from pal.channel.endpoints import SocketChannelEndpoint
from pal.channel.endpoints.socket_protocol import pack_socket_message, read_socket_message
from pal.channel.capabilities import ChannelIntrospectionProvider
from pal.control import InteractionButtonSpec, InteractionMessageSpec
from pal.core.turns import EffectResult, MailboxReplyEffect, channel_turn_program
from pal.execution import CapabilityCall
from pal.foundation import EventEnvelope, StoredArtifact
from pal.core.runtime_config import RuntimeConfig
from pal.identity import DEFAULT_PERSONA_ID, IdentityRepository
from pal.llm import (
    request_ir_from_prompt,
    generation_result_from_values,
    EncryptedFileSecretStore,
    EndpointResolver,
    InMemorySecretStore,
    LLMEndpointRepository,
    LLMEndpointInvocationError,
    LLMPreflightAdvice,
    LLMPreflightRequest,
    LLMRuntime,
    LLMCredentialResolver,
    RuntimeSettingRepository,
    SecretRef,
    build_default_endpoint_invoker,
)
from pal.shared.tool_protocol import ToolCallIR, new_tool_call
from pal.memory import HashingEmbedder, L3CommitRequest, L3CorrectRequest, L3ProviderSelector, MemoryPackRequest, MemoryQuery, MemoryService
from pal.plugins.l3 import SQLiteVecL3Plugin
from pal.plugins import PluginBundleRepository
from pal.plugins.capabilities import PluginsIntrospectionProvider
from pal.proactive import ProactiveDefinition, ProactiveRepository
from pal.shared import ChannelStreamUpdate, ChannelStreamUpdateKind, IntrospectionCall, LLMFinishReason, RuntimeStatus, SINGLETON_TARGET
from pal.wizard import WizardService
from pal.web_fetch import DEFAULT_WEB_FETCH_USER_AGENT, BrowserServiceManager, WebFetchProviderRepository, plain_http_fetch
from pal.web_search import WebSearchItem, WebSearchProviderRepository
from tests.runtime_channel_providers import telegram_endpoint_module


_telegram_module = telegram_endpoint_module()
TelegramChannelEndpoint = _telegram_module.TelegramChannelEndpoint
TelegramChannelEndpointFactory = _telegram_module.TelegramChannelEndpointFactory


def _plain_telegram_segments(text: str, *, limit: int):
    _ = limit
    return [
        _telegram_module._TelegramTextSegment(
            rendered_text=str(text),
            fallback_text=str(text),
            parse_mode=None,
        )
    ]


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

    def test_wizard_leaves_bunshin_catalog_ownership_to_the_sidecar(self) -> None:
        bunshin_plugin_root = self.registration.runtime.runtime_root / "plugins" / "bunshin"

        self.assertFalse((bunshin_plugin_root / "profiles").exists())
        self.assertFalse((bunshin_plugin_root / "families").exists())

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








    def test_oauth_credential_resolver_uses_oauth_profile_ref(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="openai_oauth",
            provider="openai",
            model_id="gpt-test",
            wire_shape="openai_response",
            base_url="https://api.openai.com/v1/responses",
            auth_kind="oauth",
            credential_ref="openai_oauth",
            thinking_levels_blob=["off"],
            default_thinking_level="off",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(
            SecretRef(service="openai_oauth", account="oauth-profile"),
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
        self.assertEqual(auth.secret_ref, SecretRef(service="openai_oauth", account="oauth-profile"))
        self.assertEqual(auth.access_token, "oauth-access-token")
        self.assertEqual(auth.profile["refresh_token"], "oauth-refresh-token")
        self.assertEqual(resolver.resolve_api_key(endpoint), "oauth-access-token")































    def test_encrypted_file_secret_store_reloads_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_secret_reload_test_") as tmp:
            secrets_path = Path(tmp) / "secrets.json"
            first = EncryptedFileSecretStore(secrets_path)
            ref = SecretRef(service="example_llm", account="api-key")
            self.assertIsNone(first.get_secret(ref))

            second = EncryptedFileSecretStore(secrets_path)
            second.set_secret(ref, "bridge-token")

            self.assertEqual(first.get_secret(ref), "bridge-token")

    def test_encrypted_file_secret_store_survives_hostname_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_secret_hostname_test_") as tmp:
            secrets_path = Path(tmp) / "secrets.json"
            ref = SecretRef(service="example_llm", account="api-key")

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
            ref = SecretRef(service="example_llm", account="api-key")
            with patch("pal.llm.secret_store.socket.gethostname", return_value="current-host"):
                old_key = _derive_fernet_key(runtime_root=str(secrets_path.parent), include_hostname=True)
            encrypted = Fernet(old_key).encrypt(b"bridge-token").decode()
            secrets_path.write_text(
                json.dumps(
                    {
                        "example_llm:api-key": {
                            "service": "example_llm",
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
            endpoint_id="openai_oauth",
            provider="openai",
            model_id="gpt-test",
            wire_shape="openai_response",
            base_url="https://api.openai.com/v1/responses",
            auth_kind="oauth",
            credential_ref="openai_oauth",
            thinking_levels_blob=["off"],
            default_thinking_level="off",
            priority=0,
            enabled=True,
        )
        secret_store = InMemorySecretStore()
        secret_store.set_secret(
            SecretRef(service="openai_oauth", account="oauth-profile"),
            json.dumps({"refresh_token": "oauth-refresh-token"}),
        )
        resolver = LLMCredentialResolver(secret_store=secret_store)

        auth = resolver.resolve_auth(endpoint)

        self.assertIsNone(auth.access_token)
        self.assertIsNone(resolver.resolve_api_key(endpoint))









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
        self.assertIn("op_web_inspect_layout", canonical_paths)
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
        self.assertIn("inspect_web_layout", tool_names)
        self.assertNotIn("screenshot_web", tool_names)

    def test_compose_runtime_loads_bunshin_as_first_party_builtin_plugin(self) -> None:
        if not _local_sidecar_bind_available():
            self.skipTest("local socket binding is unavailable in this test sandbox")
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )

        records = handle.plugin_host.list_plugins()
        bunshin_record = next(item for item in records if item["plugin_id"] == "bunshin")
        self.assertEqual(bunshin_record["source"], "first_party")
        self.assertTrue(bunshin_record["attached"])
        self.assertIsNotNone(handle.core.context.module_registry.get("bunshin"))
        self.assertIn("bunshin_start_workflow", handle.core.context.capability_registry.descriptors)
        self.assertIn("bunshin_task_status", handle.core.context.capability_registry.descriptors)
        self.assertNotIn("bunshin_dispatch_workflow", handle.core.context.capability_registry.descriptors)
        search = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="op_tool_search", args={"query": "start bunshin workflow"})
        )
        bunshin_hit = self._find_search_hit_by_alias(handle.core, search, "bunshin_start_workflow")
        self.assertEqual(bunshin_hit["alias"], "bunshin_start_workflow")
        self.assertEqual(bunshin_hit["module_id"], "bunshin")
        self.assertIn("input_shape", bunshin_hit)

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
            self.assertIn("bunshin_start_workflow", handle.core.context.capability_registry.descriptors)
            self.assertNotIn("bunshin_dispatch_workflow", handle.core.context.capability_registry.descriptors)
            search = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="op_tool_search", args={"query": "start bunshin workflow"})
            )
            bunshin_hit = self._find_search_hit_by_alias(handle.core, search, "bunshin_start_workflow")
            self.assertEqual(bunshin_hit["alias"], "bunshin_start_workflow")
            self.assertEqual(bunshin_hit["module_id"], "bunshin")
            self.assertIn("input_shape", bunshin_hit)
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

    def test_plugin_rescan_capability_reports_partial_attach_failure_as_error(self) -> None:
        class FailingHost:
            @staticmethod
            def rescan_and_attach_new_first_party() -> dict[str, object]:
                return {
                    "new_first_party_plugins": ["broken"],
                    "attached_new_first_party_plugins": [],
                    "scan_errors": {},
                    "attach_errors": {"broken": "invalid tool contract"},
                }

        result = PluginsIntrospectionProvider(  # type: ignore[arg-type]
            host=FailingHost()
        ).rescan_and_attach_new_first_party(
            CapabilityCall(name="plugin_rescan_and_attach_new_first_party")
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(
            result.structured["attach_errors"],
            {"broken": "invalid tool contract"},
        )

    def test_plugin_rescan_capability_reports_scan_failure_as_error(self) -> None:
        class FailingHost:
            @staticmethod
            def rescan() -> dict[str, object]:
                return {
                    "first_party_discovered": 0,
                    "third_party_discovered": 0,
                    "scan_errors": ["broken manifest"],
                }

        result = PluginsIntrospectionProvider(  # type: ignore[arg-type]
            host=FailingHost()
        ).rescan(CapabilityCall(name="plugin_rescan"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.structured["scan_errors"], ["broken manifest"])

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
                        "from pal.execution import CapabilityCall, CapabilityResult, ToolGuidance",
                        "from pal.execution.tool_semantics import INDIRECT_NONE",
                        "from pal.shared import OPERATION_NAMESPACE, capability_action, capability_node",
                        "",
                        "@capability_node(namespace=OPERATION_NAMESPACE, scope='demo_reload', kind='module', source='test', target_kind='module')",
                        "class DemoProvider:",
                        "    @capability_action(namespace=OPERATION_NAMESPACE, scope='demo_reload', family='operation', action_name='ping', aliases=('demo_reload_ping',), guidance=ToolGuidance(purpose='Return the reload fixture version.', use_when='Testing plugin reload.', do_not_use_when='Outside this reload fixture.', failure_next_steps='Inspect the fixture plugin.'), execution=INDIRECT_NONE)",
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
            self.assertEqual(result.status, "ok", result.structured)
            first = handle.core.context.execution_runtime.execute(CapabilityCall(name="demo_reload_ping"))
            self.assertEqual(first.text, "v1")

            detached = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_detach", args={"name": "demo_reload"})
            )
            self.assertEqual(detached.status, "ok")

            time.sleep(1.1)
            write_impl("v2")
            write_runtime()
            attached = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_attach", args={"name": "demo_reload"})
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
            "bunshin": "pal.bunshin",
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
            "bunshin": ("pal.bunshin", "bunshin_start_workflow"),
            "sqlite_vec_l3": ("pal.plugins.l3", "memory_provider_show"),
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
                detached = runtime.execute(CapabilityCall(name="plugin_detach", args={"name": plugin_id}))
                self.assertEqual(detached.status, "ok", plugin_id)
                self.assertIn(capability_name, initial_registry.descriptors, plugin_id)
                self.assertNotIn(capability_name, handle.core.context.capability_registry.descriptors, plugin_id)

                probe_name = f"{reload_prefix}.__pal_hot_reload_probe__"
                sys.modules[probe_name] = types.ModuleType(probe_name)
                attached = runtime.execute(CapabilityCall(name="plugin_attach", args={"name": plugin_id}))

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
        prefixes = ("pal.bunshin", "pal.plugins_builtin.bunshin")
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
        }
        try:
            old_handle = handle.core.context.module_registry.require("bunshin")
            old_provider = old_handle.introspection_provider
            old_manager = old_handle.ports["bunshin"]
            old_pid = int(old_manager._require_manager()["manager_pid"])
            old_source = handle.core.context.event_source_registry.sources["bunshin.manager"]
            old_handler = handle.core.context.event_handler_registry.by_module["bunshin"][0][1]

            detached = handle.core.detach_module("bunshin")

            self.assertEqual(detached, "ok")
            self.assertIn("bunshin_start_workflow", initial_registry.descriptors)
            self.assertNotIn("bunshin_start_workflow", handle.core.context.capability_registry.descriptors)
            self.assertNotIn("bunshin.manager", handle.core.context.event_source_registry.sources)
            self.assertNotIn("bunshin", handle.core.context.event_handler_registry.by_module)
            self.assertFalse(old_manager.client.socket_path.exists())
            record = next(item for item in handle.plugin_host.list_plugins() if item["plugin_id"] == "bunshin")
            self.assertFalse(record["attached"])

            probe_name = "pal.bunshin.__pal_hot_reload_probe__"
            sys.modules[probe_name] = types.ModuleType(probe_name)

            reattached = handle.core.reattach_module("bunshin")

            self.assertEqual(reattached, "ok")
            self.assertNotIn(probe_name, sys.modules)
            self.assertIn("bunshin_start_workflow", handle.core.context.capability_registry.descriptors)
            new_handle = handle.core.context.module_registry.require("bunshin")
            new_manager = new_handle.ports["bunshin"]
            new_pid = int(new_manager._require_manager()["manager_pid"])
            new_source = handle.core.context.event_source_registry.sources["bunshin.manager"]
            new_handler = handle.core.context.event_handler_registry.by_module["bunshin"][0][1]
            self.assertIsNot(new_handle.introspection_provider, old_provider)
            self.assertNotEqual(new_pid, old_pid)
            self.assertIsNot(new_source, old_source)
            self.assertIsNot(new_handler, old_handler)
            record = next(item for item in handle.plugin_host.list_plugins() if item["plugin_id"] == "bunshin")
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
            self.assertIn("memory_provider_show", handle.core.context.capability_registry.descriptors)

            detached = handle.core.context.execution_runtime.execute(
                CapabilityCall(name="plugin_detach", args={"name": "sqlite_vec_l3"})
            )

            self.assertEqual(detached.status, "ok")
            self.assertNotIn("memory_provider_show", handle.core.context.capability_registry.descriptors)
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
            CapabilityCall(name="plugin_detach", args={"name": "sqlite_vec_l3"})
        )

        self.assertEqual(detached.status, "ok")
        self.assertEqual(calls, ["cleanup"])
        self.assertEqual(module.cleanup_callbacks, [])

    def test_failed_plugin_detach_preserves_attached_runtime_generation(self) -> None:
        self.wizard.seed_defaults(self.registration)
        runtime = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        module = runtime.core.context.module_registry.require("l3.sqlite_vec_l3")
        provider = module.introspection_provider
        self.assertIsNotNone(provider)
        record = runtime.plugin_host.first_party_records["sqlite_vec_l3"]
        cleanup_calls: list[str] = []
        module.cleanup_callbacks.append(lambda: cleanup_calls.append("cleanup"))
        published = tuple(module.published_capabilities)

        with patch.object(provider, "detach", side_effect=RuntimeError("detach failed")):
            result = runtime.plugin_host.detach("sqlite_vec_l3")
            disabled = runtime.plugin_host.disable("sqlite_vec_l3")

        self.assertEqual(result["status"], "error")
        self.assertEqual(disabled["status"], "error")
        self.assertTrue(disabled["enabled"])
        self.assertTrue(record.attached)
        self.assertTrue(record.enabled)
        self.assertNotIn("sqlite_vec_l3", runtime.plugin_host.first_party_disabled)
        self.assertTrue(module.mounted)
        self.assertEqual(cleanup_calls, [])
        self.assertEqual(tuple(module.published_capabilities), published)
        for alias in published:
            self.assertIn(alias, runtime.core.context.capability_registry.descriptors)

    def test_failed_plugin_attach_rolls_back_every_runtime_projection(self) -> None:
        self.wizard.seed_defaults(self.registration)
        runtime = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        module = runtime.core.context.module_registry.require("l3.sqlite_vec_l3")
        event_kind = "test.plugin.rollback"
        action_kind = "test.plugin.rollback.action"
        event_handler = object()
        cleanup_calls: list[str] = []
        runtime.core.context.event_handler_registry.register(
            event_kind,
            event_handler,  # type: ignore[arg-type]
            module_id=module.module_id,
        )
        runtime.core.context.control_action_registry.register(
            module.module_id,
            action_kind,
            lambda _action: None,
        )
        module.cleanup_callbacks.append(lambda: cleanup_calls.append("cleanup"))

        runtime.plugin_host._rollback_failed_attach(module)

        self.assertNotIn(module.module_id, runtime.core.context.event_handler_registry.by_module)
        self.assertNotIn(event_kind, runtime.core.context.event_handler_registry.handlers)
        self.assertNotIn(module.module_id, runtime.core.context.control_action_registry.by_module)
        self.assertNotIn(action_kind, runtime.core.context.control_action_registry.handlers)
        self.assertEqual(cleanup_calls, ["cleanup"])
        self.assertEqual(module.cleanup_callbacks, [])
        self.assertFalse(module.mounted)
        self.assertTrue(module.degraded)

    def test_failed_plugin_attach_discards_spent_handle_before_retry(self) -> None:
        self.wizard.seed_defaults(self.registration)
        runtime = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        self.assertEqual(runtime.plugin_host.detach("sqlite_vec_l3")["status"], "ok")

        with patch.object(
            runtime.plugin_host,
            "_publish_module_capabilities",
            side_effect=RuntimeError("publish failed"),
        ):
            failed = runtime.plugin_host.attach("sqlite_vec_l3")

        self.assertEqual(failed["status"], "error")
        self.assertNotIn("sqlite_vec_l3", runtime.plugin_host.first_party_handles)
        self.assertIsNone(runtime.core.context.module_registry.get("l3.sqlite_vec_l3"))

        retried = runtime.plugin_host.attach("sqlite_vec_l3")
        self.assertEqual(retried["status"], "ok")
        self.assertIn("sqlite_vec_l3", runtime.plugin_host.first_party_handles)
        self.assertIsNotNone(runtime.core.context.module_registry.get("l3.sqlite_vec_l3"))

    def test_plugin_attach_detach_lifecycle_works_end_to_end(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        runtime = handle.core.context.execution_runtime
        plugin_host = handle.plugin_host

        # Verify sqlite_vec_l3 starts attached with capabilities
        initial_module = handle.core.context.module_registry.require("l3.sqlite_vec_l3")
        l3_caps_before = tuple(initial_module.published_capabilities)
        self.assertTrue(l3_caps_before, "L3 plugin should have capabilities on boot")
        for name in l3_caps_before:
            self.assertIn(name, handle.core.context.capability_registry.descriptors)
        self.assertIsNotNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # DETACH
        detached = runtime.execute(
            CapabilityCall(name="plugin_detach", args={"name": "sqlite_vec_l3"})
        )
        self.assertEqual(detached.status, "ok")

        # Capabilities withdrawn
        for name in l3_caps_before:
            self.assertNotIn(name, handle.core.context.capability_registry.descriptors)

        # Provider ref removed
        self.assertIsNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # Record shows detached
        records = plugin_host.list_plugins()
        l3_record = next(r for r in records if r["plugin_id"] == "sqlite_vec_l3")
        self.assertFalse(l3_record["attached"])

        # RE-ATTACH
        attached = runtime.execute(
            CapabilityCall(name="plugin_attach", args={"name": "sqlite_vec_l3"})
        )
        self.assertEqual(attached.status, "ok")

        # Capabilities restored
        reattached_module = handle.core.context.module_registry.require("l3.sqlite_vec_l3")
        self.assertEqual(
            set(reattached_module.published_capabilities),
            set(l3_caps_before),
            "All L3 capabilities should be restored after re-attach",
        )
        for name in l3_caps_before:
            self.assertIn(name, handle.core.context.capability_registry.descriptors)

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
                    "summary": "Recovered the bunshin after memory pressure crash.",
                    "search_text": "Bunshin crashed under memory pressure. Restarted the bunshin and reduced concurrency. Queue drain recovered and latency normalized.",
                    "situation_text": "Bunshin crashed under memory pressure",
                    "task_text": "Stabilize the bunshin",
                    "action_text": "Restarted it and reduced concurrency",
                    "result_text": "Latency normalized",
                    "topics": ["bunshin", "stability"],
                },
            )
        )
        mem_ref = committed.structured["mem_ref"]

        recalled = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_recall",
                args={
                    "queries": ["bunshin memory pressure stabilize"],
                    "limit": 4,
                },
            )
        )
        recalled_origin = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_memory_recall",
                args={
                    "queries": ["bunshin memory pressure stabilize"],
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
                    "summary": "Recovered the bunshin after memory pressure.",
                    "topics": ["bunshin", "recovery"],
                },
            )
        )
        inventory = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="memory_provider_inventory",
                args={"name": "sqlite_vec_l3"},
            )
        )

        self.assertEqual(committed.status, "ok")
        self.assertEqual(recalled.status, "ok")
        self.assertEqual(recalled_origin.status, "ok")
        self.assertEqual(corrected.status, "ok")
        self.assertEqual(recalled.structured["hit_count"], 1)
        self.assertEqual(recalled.structured["hits_preview"][0]["mem_ref"], mem_ref)
        self.assertEqual(recalled.structured["hits_preview"][0]["summary"], "Recovered the bunshin after memory pressure crash.")
        self.assertEqual(recalled.structured["view"], "summary")
        self.assertNotIn("hits", recalled.structured)
        self.assertNotIn("projected_entries", recalled.structured)
        self.assertNotIn("projected_entries", recalled.llm_text)
        self.assertNotIn("Restarted the bunshin and reduced concurrency.", recalled.llm_text)
        self.assertIn("Recovered the bunshin after memory pressure crash.", recalled.llm_text)
        self.assertEqual(recalled_origin.structured["view"], "origin")
        self.assertEqual(recalled_origin.structured["hit_count"], 1)
        self.assertEqual(recalled_origin.structured["hits_preview"][0]["mem_ref"], mem_ref)
        self.assertIn("Restarted the bunshin", recalled_origin.structured["hits_preview"][0]["search_text"])
        self.assertNotIn("hits", recalled_origin.structured)
        self.assertNotIn("projected_entries", recalled_origin.structured)
        self.assertIn("Restarted the bunshin and reduced concurrency.", recalled_origin.llm_text)
        self.assertNotIn("projected_entries", recalled_origin.llm_text)
        self.assertEqual(inventory.status, "ok")
        self.assertEqual(inventory.structured["provider_id"], "sqlite_vec_l3")
        self.assertIn(mem_ref, handle.memory_service.l2_store.items)
        self.assertIn(mem_ref, handle.memory_service.l2_store.heat_registry)
        self.assertEqual(handle.memory_service.l2_store.items[mem_ref].summary, "Recovered the bunshin after memory pressure.")

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
                title="Recover bunshin",
                summary="Recovered the bunshin after memory pressure crash.",
                search_text="Bunshin crashed under memory pressure. Restarted the bunshin and reduced concurrency. Queue drain recovered and latency normalized.",
                situation_text="Bunshin crashed under memory pressure",
                task_text="Stabilize the bunshin",
                action_text="Restarted it and reduced concurrency",
                result_text="Latency normalized",
                topics=["bunshin", "stability"],
            )
        )

        recall = provider.recall(MemoryQuery(queries=["bunshin memory pressure stabilize"], limit=4))

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
                title="Restart flaky bunshin",
                summary="Restarted flaky bunshin after memory pressure crash.",
                search_text="Bunshin crashed after high memory pressure. Task was to stabilize the background bunshin. Restarted the bunshin and reduced concurrency. Queue drain recovered and latency normalized.",
                situation_text="Bunshin crashed after high memory pressure",
                task_text="Stabilize the background bunshin",
                action_text="Restarted the bunshin and reduced concurrency",
                result_text="Queue drain recovered and latency normalized",
                topics=["bunshin", "stability"],
            )
        )
        provider.refresh_indexes(limit=8)

        st_hit = provider.recall(MemoryQuery(queries=["bunshin high memory pressure stabilize"], limit=4))
        ar_only = provider.recall(MemoryQuery(queries=["latency normalized"], limit=4))

        self.assertEqual(st_hit.hits[0]["document_id"], result.document_id)
        self.assertIn("Action: Restarted the bunshin", st_hit.hits[0]["rendered"])
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
                CapabilityCall(name="channel_reload_provider", args={"name": "socket_default"})
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
                CapabilityCall(name="channel_detach", args={"name": "socket_default"})
            )
            self.assertEqual(blocked.status, "invalid")
            self.assertEqual(blocked.structured["reason"], "recovery_socket_control_channel")
            self.assertIs(handle.channel_runtime.get_endpoint("socket_default"), new_endpoint)
            self.assertTrue(new_endpoint.attached)

            blocked_disable = runtime.execute(
                CapabilityCall(name="channel_disable", args={"name": "socket_default"})
            )
            self.assertEqual(blocked_disable.status, "invalid")
            self.assertEqual(blocked_disable.structured["reason"], "recovery_socket_control_channel")
            self.assertTrue(new_endpoint.enabled)

            detached = runtime.execute(
                CapabilityCall(name="channel_detach", args={"name": "socket_aux"})
            )
            self.assertEqual(detached.status, "ok")
            detached_endpoint = handle.channel_runtime.get_endpoint("socket_aux")
            self.assertIsNone(detached_endpoint)
            self.assertFalse(old_aux_endpoint.attached)

            attached = runtime.execute(
                CapabilityCall(name="channel_attach", args={"name": "socket_aux"})
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

    def test_channel_provider_rescan_restores_running_endpoints_without_attach_flag(self) -> None:
        self.wizard.seed_defaults(self.registration)
        ChannelEndpointRepository().upsert(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat:123",
            enabled=True,
            supports_typing=True,
            supports_receipt_marker=True,
            binding_metadata={},
            send_policy_blob={},
        )
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        old_endpoint = handle.channel_runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(old_endpoint)
        old_endpoint.bot_token = "runtime-only-token"

        result = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="channel_provider_rescan")
        )

        self.assertEqual(result.status, "ok")
        endpoint = handle.channel_runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(endpoint)
        self.assertIsNot(endpoint, old_endpoint)
        self.assertTrue(endpoint.attached)
        self.assertEqual(endpoint.bot_token, "runtime-only-token")
        self.assertIn("telegram_main", result.structured["restored_endpoint_ids"])

    def test_channel_provider_rescan_transfers_pending_reply_ownership(self) -> None:
        repository = ChannelEndpointRepository()
        repository.upsert(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat:123",
            enabled=True,
            binding_metadata={},
        )
        runtime = ChannelRuntime()
        provider = ChannelIntrospectionProvider(
            runtime=runtime,
            repository=repository,
            runtime_root=self.runtime_root,
        )
        provider.provider_manager.load_runtime_providers()
        provider.provider_manager.hydrate_all()
        old_endpoint = runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(old_endpoint)
        pending_reply_id = old_endpoint.queue_reply(
            ChannelMessage(
                text="reply queued before provider rescan",
                tag="checklist",
                payload={"action": "upsert", "active": True, "total": 1},
            ),
            response_handle=old_endpoint.build_response_handle(
                reply_target={"chat_id": "123"}
            ),
        )

        result = provider.provider_manager.rescan_providers()

        self.assertFalse(result["scan_errors"])
        endpoint = runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(endpoint)
        self.assertIsNot(endpoint, old_endpoint)
        self.assertEqual(
            [item.reply_id for item in endpoint.outbox],
            [pending_reply_id],
        )
        self.assertEqual(endpoint.outbox[0].tag, "checklist")
        self.assertEqual(endpoint.outbox[0].payload["action"], "upsert")
        self.assertFalse(old_endpoint.outbox)

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
                name="channel_endpoint_health",
                args={"name": "demo_runtime_main"},
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
        self.assertIn("channel_endpoint_health", result.structured["republished_capability_names"])
        endpoint = handle.channel_runtime.get_endpoint("demo_runtime_main")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.__class__.__name__, "DemoRuntimeEndpoint")

        health = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="channel_endpoint_health",
                args={"name": "demo_runtime_main"},
            )
        )

        self.assertEqual(health.status, "ok")
        self.assertEqual(health.structured["source"], "runtime_root_provider")

    def test_channel_provider_rescan_failure_restores_previous_generation(self) -> None:
        self.wizard.seed_defaults(self.registration)
        handle = self._compose_runtime(
            wizard=self.wizard,
            registration=self.registration,
            database=self.database,
        )
        self._write_demo_runtime_channel_provider()
        ChannelEndpointRepository().upsert(
            endpoint_id="demo_runtime_main",
            channel_kind="demo_runtime",
            binding_key="demo:1",
            enabled=True,
        )
        attached = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="channel_provider_rescan",
                args={"attach_enabled_endpoints": True},
            )
        )
        self.assertEqual(attached.status, "ok")
        old_endpoint = handle.channel_runtime.get_endpoint("demo_runtime_main")
        self.assertIsNotNone(old_endpoint)
        provider_path = self.runtime_root / "channel" / "providers" / "demo_runtime" / "runtime.py"
        provider_path.write_text("this is not valid python !!!\n", encoding="utf-8")

        failed = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="channel_provider_rescan",
                args={"attach_enabled_endpoints": False},
            )
        )

        self.assertEqual(failed.status, "error")
        self.assertTrue(failed.structured["runtime_provider_load_errors"])
        self.assertIn("demo_runtime", failed.structured["runtime_provider_ids"])
        self.assertIs(handle.channel_runtime.get_endpoint("demo_runtime_main"), old_endpoint)

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
        self.assertFalse(getattr(new_endpoint, "drop_pending_updates_on_start", True))

    def test_channel_attach_factory_failure_preserves_detached_repository_state(self) -> None:
        repository = ChannelEndpointRepository()
        repository.upsert(
            endpoint_id="missing_runtime",
            channel_kind="demo",
            binding_key="demo:missing",
        )
        repository.set_attached("missing_runtime", False)
        runtime = ChannelRuntime()
        provider = FactoryChannelProvider(
            provider_id="demo",
            endpoint_types=("demo",),
            factory=types.SimpleNamespace(create=lambda _record, runtime_root: None),
        )
        context = ChannelProviderContext(runtime=runtime, repository=repository, runtime_root=self.runtime_root)

        result = provider.attach_endpoint("missing_runtime", context)

        self.assertEqual(result.status, "not_found")
        self.assertIsNotNone(repository.get("missing_runtime").detached_at)
        self.assertIsNone(runtime.get_endpoint("missing_runtime"))

    def test_channel_detach_repository_failure_restores_runtime_endpoint(self) -> None:
        repository = ChannelEndpointRepository()
        repository.upsert(
            endpoint_id="demo_main",
            channel_kind="telegram",
            binding_key="demo:1",
        )
        runtime = ChannelRuntime()
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="demo_main", channel_kind="telegram", binding_key="demo:1"),
            runtime_root=self.runtime_root,
            bot_token="",
        )
        runtime.register_endpoint(endpoint)
        provider = FactoryChannelProvider(
            provider_id="demo",
            endpoint_types=("telegram",),
            factory=types.SimpleNamespace(create=lambda record, runtime_root: endpoint),
        )
        context = ChannelProviderContext(runtime=runtime, repository=repository, runtime_root=self.runtime_root)

        with patch.object(repository, "set_attached", side_effect=RuntimeError("database failed")):
            with self.assertRaisesRegex(RuntimeError, "database failed"):
                provider.detach_endpoint("demo_main", context)

        self.assertIs(runtime.get_endpoint("demo_main"), endpoint)
        self.assertTrue(endpoint.attached)

    def test_channel_enable_repository_failure_restores_runtime_flag(self) -> None:
        repository = ChannelEndpointRepository()
        repository.upsert(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            binding_key="chat:1",
            enabled=False,
        )
        runtime = ChannelRuntime()
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:1"),
            runtime_root=self.runtime_root,
            bot_token="",
        )
        endpoint.enabled = False
        runtime.register_endpoint(endpoint)
        provider = ChannelIntrospectionProvider(
            runtime=runtime,
            repository=repository,
            runtime_root=self.runtime_root,
        )

        with patch.object(repository, "set_enabled", side_effect=RuntimeError("database failed")):
            result = provider._set_enabled(
                IntrospectionCall(name="channel.enable", args={"name": "telegram_main"}),
                enabled=True,
            )

        self.assertEqual(result.status, "error")
        self.assertFalse(endpoint.enabled)
        self.assertFalse(repository.get("telegram_main").enabled)


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
                name="web_search_provider_set_auth_material",
                args={
                    "name": "brave_search_default",
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
                name="web_fetch_provider_health",
                args={"name": "playwright_fetch_default"},
            )
        )

        self.assertEqual(health.status, "ok")
        self.assertIsNone(service.browser_manager._process)
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
                name="web_fetch_provider_disable",
                args={"name": "playwright_fetch_default"},
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
    def test_channel_turn_marks_tool_round_text_as_stream_companion(self) -> None:
        opening = EventEnvelope(
            event_kind="user.message",
            source_kind="channel",
            payload={"text": "inspect"},
        )
        program = channel_turn_program(opening)
        try:
            next(program)
            program.send(
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=LLMPreflightAdvice(status="ready"),
                )
            )
            effect = program.send(
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        text="I will inspect.",
                        tool_calls=[
                            new_tool_call(
                                name="read_file",
                                args={"file_path": "a"},
                            )
                        ],
                        finish_reason="tool_calls",
                    ),
                )
            )
        finally:
            program.close()

        self.assertIsInstance(effect, MailboxReplyEffect)
        self.assertFalse(effect.terminal)
        self.assertTrue(effect.stream_companion)

    def test_channel_turn_never_emits_an_empty_terminal_reply(self) -> None:
        opening = EventEnvelope(
            event_kind="user.message",
            source_kind="channel",
            payload={"text": "hello"},
        )
        program = channel_turn_program(opening)
        try:
            next(program)
            program.send(
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=LLMPreflightAdvice(status="ready"),
                )
            )
            effect = program.send(
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        text="",
                        tool_calls=[],
                        finish_reason="stop",
                    ),
                )
            )
        finally:
            program.close()

        self.assertIsInstance(effect, MailboxReplyEffect)
        self.assertTrue(effect.terminal)
        self.assertIn("without producing a final answer", effect.text)

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

        endpoint.send_stream_update(
            response_handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="pong"),
        )
        endpoint.queue_reply("pong", response_handle=response_handle)

        self.assertEqual(outbound.items, [{"type": "text_delta", "request_id": "req-1", "text": "pong"}])
        self.assertFalse(endpoint.outbox)
        self.assertNotIn(id(response_handle), endpoint._streamed_text_handles)

    def test_socket_terminal_canonical_text_repairs_missing_deltas(self) -> None:
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key="pal.sock",
            )
        )
        outbound = _OutboundQueue()
        endpoint.sessions["session-1"] = type("Session", (), {"outbound": outbound, "closed": False})()
        handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={"session_id": "session-1", "request_id": "req-1"},
        )

        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.PROGRESS,
                text="Checklist: inspected",
            ),
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                text="canonical final",
                finish_reason="stop",
            ),
        )

        self.assertEqual(
            outbound.items,
            [
                {"type": "text_delta", "request_id": "req-1", "text": "Checklist: inspected"},
                {"type": "text_delta", "request_id": "req-1", "text": "canonical final"},
                {
                    "type": "llm_done",
                    "request_id": "req-1",
                    "final_text": "canonical final",
                    "finish_reason": "stop",
                },
            ],
        )
        self.assertFalse(endpoint._stream_sessions)

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

        endpoint.send_stream_update(
            stream_handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="pong"),
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

        endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="pong"),
            response_handle=stream_handle,
        )
        endpoint.queue_reply("pong", response_handle=final_handle)

        self.assertEqual(len(endpoint.stream_update_outbox), 1)
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

        endpoint.send_stream_update(
            response_handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="pong"),
        )
        endpoint.queue_reply("pong", response_handle=response_handle)

        self.assertEqual(len(endpoint.outbox), 1)
        self.assertEqual(endpoint.outbox[0].text, "pong")

    def test_telegram_buffers_tool_rounds_and_delivers_only_terminal_answer(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:42",
            )
        )
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "42"},
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="I will inspect."),
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TOOL_CALL,
                tool_call=new_tool_call(name="read_file", args={"file_path": "a"}),
            ),
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.DONE, finish_reason="tool_calls"),
        )
        self.assertFalse(endpoint._turn_stream_text)

        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="Done."),
        )
        self.assertEqual(next(iter(endpoint._turn_stream_text.values())), "Done.")
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                text="Done.",
                finish_reason="stop",
            ),
        )

        self.assertFalse(endpoint._turn_stream_text)
        self.assertFalse(endpoint.outbox)

    def test_telegram_terminal_reply_wins_when_final_stream_is_still_queued(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:42",
            )
        )
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "42", "message_id": "100"},
        )

        # A completed tool round and the final answer share one ordered stream;
        # no second final-reply queue participates in delivery.
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TOOL_CALL,
                tool_call=new_tool_call(name="read_file", args={"file_path": "a"}),
            ),
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.DONE, finish_reason="tool_calls"),
        )
        endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="Done."),
            response_handle=handle,
        )
        endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.DONE, finish_reason="stop"),
            response_handle=handle,
        )

        endpoint.flush_stream_update_outbox()

        self.assertFalse(endpoint.outbox)
        self.assertFalse(endpoint._turn_stream_text)

    def test_telegram_nonterminal_tool_echo_remains_user_visible(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:42",
            )
        )
        echo_handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={
                "chat_id": "42",
                "_pal_turn_continues": True,
            },
        )

        endpoint.queue_reply("Checklist: 1/3 complete", response_handle=echo_handle)

        self.assertEqual(
            [item.text for item in endpoint.outbox],
            ["Checklist: 1/3 complete"],
        )

    def test_telegram_interrupt_retires_batched_stream_text(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:42",
            )
        )
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "42", "message_id": "100"},
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="partial"),
        )

        endpoint.abort_stream(handle)

        self.assertFalse(endpoint._turn_stream_text)

    def test_telegram_tool_only_round_does_not_suppress_later_terminal_answer(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="chat:42",
            )
        )
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "42"},
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TOOL_CALL,
                tool_call=new_tool_call(name="read_file", args={"file_path": "a"}),
            ),
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                finish_reason="tool_calls",
            ),
        )

        # Tool-only rounds do not enqueue an intermediate reply.  The next
        # stream item must open a fresh buffered round mechanically.
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TEXT_DELTA,
                text="Done.",
            ),
        )
        endpoint.send_stream_update(
            handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                text="Done.",
                finish_reason="stop",
            ),
        )

        self.assertFalse(endpoint.outbox)
        self.assertFalse(endpoint._turn_stream_text)


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

    async def test_socket_endpoint_terminal_stream_needs_no_second_final_reply(self) -> None:
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

            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="po"),
                response_handle=envelope.response_handle,
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="ng"),
                response_handle=envelope.response_handle,
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(kind=ChannelStreamUpdateKind.DONE, finish_reason="stop"),
                response_handle=envelope.response_handle,
            )
            self.assertEqual(self.endpoint.flush_stream_update_outbox(), [])

            first = await read_socket_message(reader)
            second = await read_socket_message(reader)
            third = await read_socket_message(reader)
            self.assertEqual(first["type"], "text_delta")
            self.assertEqual(first["text"], "po")
            self.assertEqual(second["type"], "text_delta")
            self.assertEqual(second["text"], "ng")
            self.assertEqual(third["type"], "llm_done")

            self.assertFalse(self.endpoint.outbox)
            self.assertFalse(self.endpoint._stream_sessions)
            self.assertFalse(self.endpoint._streamed_text_keys)
            self.assertFalse(self.endpoint._stream_handle_ids_by_key)
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

    async def test_socket_endpoint_stop_is_bounded_with_connected_client(self) -> None:
        await self.endpoint.start_async()
        _reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        deadline = asyncio.get_running_loop().time() + 1.0
        while not self.endpoint.sessions and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        await asyncio.wait_for(self.endpoint.stop_async(), timeout=2.0)

        self.assertIsNone(self.endpoint.server)
        self.assertFalse(self.endpoint.sessions)
        writer.close()

    async def test_socket_endpoint_rebinds_single_client_reply_after_generation_swap(self) -> None:
        runtime = ChannelRuntime()
        runtime.register_endpoint(self.endpoint)
        await runtime.start_async()
        _reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        writer.write(
            pack_socket_message(
                {"type": "user_message", "request_id": "req-reload", "text": "rescan"}
            )
        )
        await writer.drain()
        envelopes = await self._poll_until_envelopes()
        handle = envelopes[0].response_handle
        replacement = SocketChannelEndpoint(
            endpoint=self.endpoint.endpoint,
            socket_path=self.socket_path,
        )

        try:
            await asyncio.wait_for(runtime.replace_endpoint_async(replacement), timeout=2.0)
            replacement.queue_reply("rescan complete", response_handle=handle)
            failed = replacement.flush_outbox()
            self.assertFalse(failed[0].payload.get("permanent"))
            self.assertEqual(len(replacement.outbox), 1)

            reader2, writer2 = await asyncio.open_unix_connection(str(self.socket_path))
            try:
                deadline = asyncio.get_running_loop().time() + 1.0
                while not replacement.sessions and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                delivered = replacement.flush_outbox()
                self.assertEqual([event.event_kind for event in delivered], ["reply.delivered"])
                self.assertEqual((await read_socket_message(reader2))["type"], "text_delta")
                self.assertEqual((await read_socket_message(reader2))["type"], "done")
            finally:
                writer2.close()
                await writer2.wait_closed()
        finally:
            writer.close()
            await runtime.stop_async()


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

    async def delete_message(self, **kwargs):
        self.actions.append(("delete_message", dict(kwargs)))

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

    async def test_checklist_tag_sends_edits_and_clears_one_native_message(self) -> None:
        handle = self.endpoint.build_response_handle(
            reply_target={"chat_id": "42", "thread_id": "7"},
        )
        created = ChannelMessage(
            text="Checklist progress 0/2\n⬜ inspect\n⬜ change",
            tag="checklist",
            payload={"action": "upsert", "active": True, "done": 0, "total": 2},
        )
        await self.endpoint._send_channel_message_async(handle, created)

        messages = [payload for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_thread_id"], 7)
        target = self.endpoint._tagged_message_targets[("42", "7", "checklist")]

        checked = ChannelMessage(
            text="Checklist progress 1/2\n✅ inspect\n⬜ change",
            tag="checklist",
            payload={"action": "check", "active": True, "done": 1, "total": 2},
        )
        await self.endpoint._send_channel_message_async(handle, checked)

        edits = [payload for kind, payload in self.fake_bot.actions if kind == "edit_message_text"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], target["message_id"])
        self.assertEqual(len([1 for kind, _ in self.fake_bot.actions if kind == "message"]), 1)

        cleared = ChannelMessage(
            text="Checklist cleared.",
            tag="checklist",
            payload={"action": "clear", "active": False},
        )
        await self.endpoint._send_channel_message_async(handle, cleared)

        deletes = [payload for kind, payload in self.fake_bot.actions if kind == "delete_message"]
        self.assertEqual(deletes, [{"chat_id": 42, "message_id": target["message_id"]}])
        self.assertNotIn(("42", "7", "checklist"), self.endpoint._tagged_message_targets)

    async def test_unknown_message_tag_falls_back_to_ordinary_text(self) -> None:
        handle = self.endpoint.build_response_handle(reply_target={"chat_id": "42"})
        await self.endpoint._send_channel_message_async(
            handle,
            ChannelMessage(text="plain fallback", tag="future_widget", payload={"x": 1}),
        )
        sent = [payload for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertEqual([item["text"] for item in sent], ["plain fallback"])

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

    async def test_telegram_endpoint_delivers_terminal_reply_after_tool_round_queue_race(self) -> None:
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "100", "message_id": "10"},
        )
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.TOOL_CALL,
                tool_call=new_tool_call(name="read_file", args={"file_path": "a"}),
            ),
            response_handle=handle,
        )
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.DONE, finish_reason="tool_calls"),
            response_handle=handle,
        )
        self.endpoint.flush_stream_update_outbox()

        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.PROGRESS,
                text="Checklist: inspected",
            ),
            response_handle=handle,
        )
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="Done."),
            response_handle=handle,
        )
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                text="Done.",
                finish_reason="stop",
            ),
            response_handle=handle,
        )
        with patch.object(
            _telegram_module,
            "_telegram_text_segments",
            side_effect=_plain_telegram_segments,
        ):
            self.endpoint.flush_stream_update_outbox()
            pending = next(iter(self.endpoint._send_chains.values()))
            await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)

        messages = [payload for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertEqual(
            [item["text"] for item in messages],
            ["Checklist: inspected", "Done."],
        )
        self.assertFalse(self.endpoint.outbox)

    async def test_telegram_endpoint_serializes_replies_for_same_thread(self) -> None:
        self.fake_bot.message_delays["first"] = 0.05
        handle = self.endpoint.build_response_handle(
            reply_target={"chat_id": "100", "message_id": "10", "thread_id": ""},
        )

        with patch.object(_telegram_module, "_telegram_text_segments", side_effect=_plain_telegram_segments):
            self.endpoint.queue_reply("first", response_handle=handle)
            self.endpoint.queue_reply("second", response_handle=handle)
            self.endpoint.flush_outbox()
            pending = next(iter(self.endpoint._send_chains.values()))
            await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)

        sent_texts = [str(payload.get("text") or "").strip() for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertEqual(sent_texts[-2:], ["first", "second"])

    async def test_telegram_reply_is_acknowledged_only_after_network_send_finishes(self) -> None:
        self.fake_bot.message_delays["slow"] = 0.05
        handle = self.endpoint.build_response_handle(reply_target={"chat_id": "100"})
        self.endpoint.queue_reply("slow", response_handle=handle)

        first = self.endpoint.flush_outbox()

        self.assertEqual(first, [])
        self.assertEqual(len(self.endpoint._pending_reply_deliveries), 1)
        pending = next(iter(self.endpoint._pending_reply_deliveries.values()))[1]
        await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)

        delivered = self.endpoint.flush_outbox()

        self.assertEqual([event.event_kind for event in delivered], ["reply.delivered"])
        self.assertFalse(self.endpoint._pending_reply_deliveries)

    async def test_telegram_network_failure_requeues_reply_instead_of_false_delivery(self) -> None:
        async def fail_send(**_kwargs):
            raise RuntimeError("telegram network down")

        self.fake_bot.send_message = fail_send  # type: ignore[method-assign]
        handle = self.endpoint.build_response_handle(reply_target={"chat_id": "100"})
        self.endpoint.queue_reply(
            ChannelMessage(
                text="retry me",
                tag="checklist",
                payload={"action": "check", "done": 1, "total": 2},
            ),
            response_handle=handle,
        )
        self.assertEqual(self.endpoint.flush_outbox(), [])
        pending = next(iter(self.endpoint._pending_reply_deliveries.values()))[1]
        await asyncio.gather(pending, return_exceptions=True)

        failed = self.endpoint.flush_outbox()

        self.assertEqual([event.event_kind for event in failed], ["reply.failed"])
        self.assertEqual(len(self.endpoint.outbox), 1)
        self.assertEqual(self.endpoint.outbox[0].text, "retry me")
        self.assertEqual(self.endpoint.outbox[0].tag, "checklist")
        self.assertEqual(self.endpoint.outbox[0].payload["done"], 1)

    async def test_telegram_send_chain_does_not_overtake_failed_reply(self) -> None:
        original_send = self.fake_bot.send_message

        async def fail_first(**kwargs):
            if str(kwargs.get("text") or "").strip() == "first":
                raise RuntimeError("first reply failed")
            return await original_send(**kwargs)

        self.fake_bot.send_message = fail_first  # type: ignore[method-assign]
        handle = self.endpoint.build_response_handle(reply_target={"chat_id": "100"})
        self.endpoint.queue_reply("first", response_handle=handle)
        self.endpoint.queue_reply("second", response_handle=handle)
        self.endpoint.flush_outbox()
        tasks = [task for _, task in self.endpoint._pending_reply_deliveries.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

        failed = self.endpoint.flush_outbox()

        self.assertEqual(len(failed), 2)
        self.assertEqual([item.text for item in self.endpoint.outbox], ["first", "second"])
        self.assertFalse(self.endpoint._send_chains)
        sent_texts = [
            str(payload.get("text") or "").strip()
            for kind, payload in self.fake_bot.actions
            if kind == "message"
        ]
        self.assertNotIn("second", sent_texts)

        self.fake_bot.send_message = original_send  # type: ignore[method-assign]
        self.endpoint.flush_outbox()
        retry_tasks = [task for _, task in self.endpoint._pending_reply_deliveries.values()]
        await asyncio.gather(*retry_tasks)
        delivered = self.endpoint.flush_outbox()

        self.assertEqual([event.event_kind for event in delivered], ["reply.delivered"] * 2)
        sent_texts = [
            str(payload.get("text") or "").strip()
            for kind, payload in self.fake_bot.actions
            if kind == "message"
        ]
        self.assertEqual(sent_texts[-2:], ["first", "second"])

    async def test_telegram_stop_recovers_in_flight_reply_for_replacement(self) -> None:
        self.fake_bot.message_delays["unfinished"] = 10.0
        handle = self.endpoint.build_response_handle(reply_target={"chat_id": "100"})
        self.endpoint.queue_reply("unfinished", response_handle=handle)
        self.endpoint.flush_outbox()
        await asyncio.sleep(0)

        await self.endpoint.stop_async()

        self.assertFalse(self.endpoint._pending_reply_deliveries)
        self.assertEqual([item.text for item in self.endpoint.outbox], ["unfinished"])

    async def test_telegram_progress_failure_requeues_stream_update(self) -> None:
        async def fail_send(**_kwargs):
            raise RuntimeError("telegram progress network down")

        self.fake_bot.send_message = fail_send  # type: ignore[method-assign]
        handle = self.endpoint.build_response_handle(reply_target={"chat_id": "100"})
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.PROGRESS,
                text="Checklist: still working",
            ),
            response_handle=handle,
        )
        self.assertEqual(self.endpoint.flush_stream_update_outbox(), [])
        pending = next(iter(self.endpoint._pending_stream_deliveries.values()))[1]
        await asyncio.gather(pending, return_exceptions=True)

        failed = self.endpoint.flush_stream_update_outbox()

        self.assertEqual([event.event_kind for event in failed], ["reply.failed"])
        self.assertEqual(len(self.endpoint.stream_update_outbox), 1)

    async def test_telegram_endpoint_batches_terminal_markdown_blocks_only_after_done(self) -> None:
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "100", "message_id": "10"},
        )
        final_text = "\n\n".join(
            f"Paragraph {index} has **important formatting**."
            for index in range(8)
        )
        midpoint = len(final_text) // 2
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text=final_text[:midpoint]),
            response_handle=handle,
        )
        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text=final_text[midpoint:]),
            response_handle=handle,
        )

        self.endpoint.flush_stream_update_outbox()

        self.assertFalse(any(kind == "message" for kind, _ in self.fake_bot.actions))
        self.assertFalse(self.endpoint._send_chains)

        self.endpoint.queue_stream_update(
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                text=final_text,
                finish_reason="stop",
            ),
            response_handle=handle,
        )
        self.endpoint.flush_stream_update_outbox()
        pending = next(iter(self.endpoint._send_chains.values()))
        await asyncio.wait_for(asyncio.shield(pending), timeout=2.0)

        messages = [payload for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(item.get("parse_mode") == "MarkdownV2" for item in messages))
        self.assertFalse(self.endpoint._turn_stream_text)

    async def test_telegram_endpoint_retries_only_failed_markdown_block_as_plain_text(self) -> None:
        handle = ResponseHandle(
            endpoint_id="telegram_main",
            reply_target={"chat_id": "100", "message_id": "10"},
        )
        reply = "\n\n".join(
            (
                "First **bold** paragraph.",
                "Second _italic_ paragraph.",
                "Third `code` paragraph.",
            )
        )
        segments = _telegram_module._telegram_text_segments(reply, limit=32)
        self.assertGreaterEqual(len(segments), 3)
        failed_rendered_text = segments[1].rendered_text
        attempts: list[dict[str, object]] = []
        original_send_message = self.fake_bot.send_message

        async def fail_markdown_once(**kwargs):
            attempts.append(dict(kwargs))
            if (
                kwargs.get("parse_mode") == "MarkdownV2"
                and kwargs.get("text") == failed_rendered_text
            ):
                raise RuntimeError("invalid markdown")
            return await original_send_message(**kwargs)

        self.fake_bot.send_message = fail_markdown_once  # type: ignore[method-assign]

        await self.endpoint._send_reply_async(handle, reply)

        self.assertEqual(len(attempts), len(segments) + 1)
        failed_attempt_index = next(
            index
            for index, attempt in enumerate(attempts)
            if attempt.get("text") == failed_rendered_text
        )
        self.assertEqual(attempts[failed_attempt_index].get("parse_mode"), "MarkdownV2")
        fallback_attempt = attempts[failed_attempt_index + 1]
        self.assertNotIn("parse_mode", fallback_attempt)
        self.assertEqual(fallback_attempt["text"], segments[1].fallback_text)
        self.assertTrue(
            any(
                attempt.get("parse_mode") == "MarkdownV2"
                for attempt in attempts[failed_attempt_index + 2 :]
            )
        )

    async def test_telegram_endpoint_preserves_progress_and_terminal_order_under_burst(self) -> None:
        turn_count = 25
        for index in range(turn_count):
            handle = ResponseHandle(
                endpoint_id="telegram_main",
                reply_target={"chat_id": "100", "message_id": str(index)},
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.TEXT_DELTA,
                    text=f"discarded draft {index}",
                ),
                response_handle=handle,
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.DONE,
                    finish_reason=LLMFinishReason.TOOL_CALLS.value,
                ),
                response_handle=handle,
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.PROGRESS,
                    text=f"progress {index}",
                ),
                response_handle=handle,
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.TEXT_DELTA,
                    text=f"final {index}",
                ),
                response_handle=handle,
            )
            self.endpoint.queue_stream_update(
                ChannelStreamUpdate(
                    kind=ChannelStreamUpdateKind.DONE,
                    text=f"final {index}",
                    finish_reason=LLMFinishReason.STOP.value,
                ),
                response_handle=handle,
            )

        with patch.object(
            _telegram_module,
            "_telegram_text_segments",
            side_effect=_plain_telegram_segments,
        ):
            self.endpoint.flush_stream_update_outbox()
            pending = next(iter(self.endpoint._send_chains.values()))
            await asyncio.wait_for(asyncio.shield(pending), timeout=5.0)

        sent_texts = [
            str(payload.get("text") or "")
            for kind, payload in self.fake_bot.actions
            if kind == "message"
        ]
        expected = [
            text
            for index in range(turn_count)
            for text in (f"progress {index}", f"final {index}")
        ]
        self.assertEqual(sent_texts, expected)
        self.assertFalse(self.endpoint._turn_stream_text)

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
        self.assertIsInstance(attachment, StoredArtifact)
        self.assertTrue(str(attachment.local_cached_path).startswith(str(self.runtime_root / "artifacts" / "telegram" / "777")))
        self.assertFalse("hello telegram" in str(attachment))
        self.assertEqual(
            attachment.metadata["source_metadata"]["source_url"],
            "https://api.telegram.org/file/bottoken-123/docs/file.txt",
        )
        self.assertNotIn("token-123", repr(attachment))

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
        self.assertEqual(attachment.metadata["source_metadata"]["source_url"], absolute)
        self.assertNotIn("/https://", attachment.metadata["source_metadata"]["source_url"].removeprefix("https://"))

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
            interaction_kind="bunshin_v2_architecture_review",
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
            interaction_kind="bunshin_v2_architecture_review",
            text="Review ready",
            buttons=(
                (
                    InteractionButtonSpec(
                        label="Accept",
                        action_key="control.action.dispatch",
                        action_args={
                            "action_kind": "bunshin_v2_human_decision",
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

    async def test_telegram_endpoint_duplicate_interactive_update_is_serialized(self) -> None:
        spec = InteractionMessageSpec(
            interaction_id="ctl_panel_duplicate",
            interaction_kind="control_panel",
            text="Select a model",
            buttons=((InteractionButtonSpec(label="Model", action_key="control.model.set"),),),
            expires_at=None,
        )
        response_handle = self.endpoint.build_response_handle(reply_target={"chat_id": "42"})
        for _ in range(2):
            self.endpoint.queue_status(
                "interactive_update",
                payload={"spec": spec},
                response_handle=response_handle,
            )

        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)

        messages = [payload for kind, payload in self.fake_bot.actions if kind == "message"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(any(kind == "edit_message_text" for kind, _ in self.fake_bot.actions))

    async def test_telegram_endpoint_not_modified_edit_is_idempotent(self) -> None:
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
        self.assertFalse(messages)
        self.assertIn("ctl_panel_stale", self.endpoint._interactive_messages)
        self.assertEqual(self.endpoint._interactive_messages["ctl_panel_stale"]["message_id"], 10)

    async def test_telegram_endpoint_stale_interaction_falls_back_to_new_message(self) -> None:
        self.endpoint._interactive_messages["ctl_panel_stale"] = {
            "chat_id": 42,
            "message_id": 10,
            "interaction_kind": "control_panel",
            "expires_at_monotonic": None,
            "actions": {},
        }

        async def _raise_edit_message_text(**kwargs):
            _ = kwargs
            raise RuntimeError("Message to edit not found")

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
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Pal Control Panel")
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

    async def test_telegram_explicit_delete_expiration_removes_interaction_message(self) -> None:
        handle = self.endpoint.build_response_handle(
            reply_target={"chat_id": "42"},
        )
        spec = InteractionMessageSpec(
            interaction_id="cache_warm_epoch_a",
            interaction_kind="cache_warm_deadline",
            text="Compact while the cache is warm.",
            buttons=(
                (
                    InteractionButtonSpec(
                        label="Compact",
                        action_key="control.compact.run",
                    ),
                ),
            ),
        )
        await self.endpoint._apply_interactive_status_async(
            handle,
            kind="interactive_open",
            payload={"spec": spec},
        )
        target = dict(self.endpoint._interactive_messages[spec.interaction_id])

        await self.endpoint._apply_interactive_status_async(
            handle,
            kind="interactive_expire",
            payload={"spec": spec, "delete": True},
        )

        deletes = [
            payload
            for kind, payload in self.fake_bot.actions
            if kind == "delete_message"
        ]
        self.assertEqual(
            deletes,
            [{"chat_id": 42, "message_id": target["message_id"]}],
        )
        self.assertNotIn(spec.interaction_id, self.endpoint._interactive_messages)

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
