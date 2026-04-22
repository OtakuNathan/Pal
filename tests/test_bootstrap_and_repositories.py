from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.bootstrap import compose_runtime
from pal.channel import ChannelEndpointRepository
from pal.channel.contracts import EndpointConfig
from pal.channel.endpoints import SocketChannelEndpoint, TelegramChannelEndpoint
from pal.channel.endpoints.socket_protocol import pack_socket_message, read_socket_message
from pal.execution import CapabilityCall
from pal.core.runtime_config import RuntimeConfig
from pal.identity import DEFAULT_PERSONA_ID, IdentityRepository
from pal.llm import (
    CanonicalLLMRequest,
    CanonicalLLMOutcome,
    DEFAULT_THINK_LEVEL,
    EndpointResolver,
    InMemorySecretStore,
    LLMEndpointRepository,
    LLMPreflightRequest,
    LLMRuntime,
    LiteLLMCredentialResolver,
    LiteLLMEndpointInvoker,
    RuntimeSettingRepository,
    SecretRef,
)
from pal.memory import HashingEmbedder, L3CommitRequest, L3CorrectRequest, L3ProviderSelector, MemoryPackRequest, MemoryQuery, MemoryService
from pal.plugins.l3 import SQLiteVecL3Plugin
from pal.plugins import PluginBundleRepository
from pal.service import ServiceDefinition, ServiceRepository
from pal.shared import LLMFinishReason, LLMStreamEventKind
from pal.stream_events import NormalizedLLMStreamEvent
from pal.supervisor import SupervisorService
from pal.web_fetch import BrowserServiceManager, WebFetchProviderRepository
from pal.web_search import WebSearchItem, WebSearchProviderRepository


class PalV2BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_bootstrap_test_"))
        self.supervisor = SupervisorService()
        self.registration = self.supervisor.provision_runtime(
            display_name="PalV2 Test",
            runtime_root=self.runtime_root,
            db_filename="pal_test.sqlite3",
            pal_entrypoint="pal.runtime.test",
        )
        self.db_path = self.registration.runtime.db_path
        self.database = self.supervisor.create_database(self.registration)

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.runtime_root, ignore_errors=True)

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
        self.assertIn("service_definitions", tables)
        self.assertIn("service_runs", tables)
        self.assertIn("web_search_providers", tables)
        self.assertIn("web_fetch_providers", tables)
        self.assertNotIn("users", tables)
        self.assertNotIn("conversation_routes", tables)
        self.assertNotIn("pal_memories", tables)

    def test_compose_runtime_includes_service_runtime(self) -> None:
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        self.assertIsNotNone(handle.service_manager)
        self.assertIsNotNone(handle.service_repository)
        self.assertIsNotNone(handle.service_runner)
        self.assertIn("service", handle.core.context.module_registry.modules)

    def test_service_repository_round_trips_definition_and_run(self) -> None:
        repository = ServiceRepository()
        definition = ServiceDefinition(
            service_id="daily_digest",
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
        self.assertEqual(stored[0].definition.service_id, "daily_digest")
        self.assertEqual(stored[0].definition.skill_refs, ["git", "summary"])
        self.assertEqual(stored[0].definition.out_reply_target, {"session_id": "session-1", "request_id": "req-1"})
        self.assertEqual(stored[0].next_due_at_utc, "2026-04-11T01:00:00+00:00")

    def test_supervisor_provision_runtime_creates_third_party_plugin_directory(self) -> None:
        self.assertTrue((self.registration.runtime.runtime_root / "plugins").is_dir())

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
        state = repository.get_pal_state()

        self.assertIsNotNone(persona)
        self.assertEqual(persona.persona_id, DEFAULT_PERSONA_ID)
        self.assertEqual(persona.display_name, "Pal")
        self.assertEqual(persona.language, "zh")
        self.assertEqual(persona.core_policy, ["Tool is the only execution primitive."])

        self.assertIsNotNone(preferences)
        self.assertEqual(preferences.timezone, "Asia/Shanghai")
        self.assertEqual(preferences.preferences_blob, {})

        self.assertIsNotNone(state)
        self.assertEqual(state.status, "idle")
        self.assertEqual(state.top_of_mind_refs, [])

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

    def test_litellm_credential_resolver_uses_secret_store_ref(self) -> None:
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
        resolver = LiteLLMCredentialResolver(secret_store=secret_store)

        secret = resolver.resolve_api_key(endpoint)

        assert endpoint is not None
        self.assertEqual(secret, "sk-test")
        self.assertEqual(resolver.secret_ref_for_endpoint(endpoint), SecretRef(service="deepseek-prod", account="api-key"))

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
        self.assertEqual(advice.breakdown["current_user_chars"], 1000)
        self.assertGreaterEqual(advice.breakdown["hard_keep_chars"], 6000)
        self.assertLess(advice.breakdown["available_input_budget_chars"], advice.breakdown["hard_keep_chars"])

    def test_runtime_setting_repository_persists_think_level(self) -> None:
        repository = RuntimeSettingRepository()

        repository.ensure_defaults()
        self.assertEqual(repository.get_think_level(), DEFAULT_THINK_LEVEL)

        repository.set_think_level("deep")
        self.assertEqual(repository.get_think_level(), "deep")

    def test_llm_runtime_injects_think_level_into_request_metadata(self) -> None:
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
        settings_repository.set_think_level("deep")
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
        self.assertEqual(runtime.last_request.metadata["think_level"], "deep")

    def test_compose_runtime_loads_first_party_sqlite_vec_plugin_via_plugin_host(self) -> None:
        self.supervisor.seed_defaults(self.registration)

        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        records = handle.plugin_host.list_plugins()
        sqlite_record = next(item for item in records if item["plugin_id"] == "sqlite_vec_l3")
        self.assertEqual(sqlite_record["source"], "first_party")
        self.assertTrue(sqlite_record["attached"])
        self.assertEqual(handle.memory_service.l3_selector.active_provider_id, "sqlite_vec_l3")
        self.assertIn("sqlite_vec_l3", handle.core.context.execution_runtime.l3_plugin_registry.plugins)

    def test_supervisor_seeds_default_web_providers_and_active_settings(self) -> None:
        self.supervisor.seed_defaults(self.registration)

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
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        records = handle.plugin_host.list_plugins()
        plugin_ids = {item["plugin_id"] for item in records if item["source"] == "first_party" and item["attached"]}
        tool_names = {item["function"]["name"] for item in handle.core._build_llm_tool_contracts()}

        self.assertIn("web_search", plugin_ids)
        self.assertIn("web_fetch", plugin_ids)
        self.assertIsNotNone(handle.core.context.module_registry.get("web_search"))
        self.assertIsNotNone(handle.core.context.module_registry.get("web_fetch"))
        self.assertIn("op_web_search_query", handle.core.context.capability_registry.descriptors)
        self.assertIn("op_web_fetch_read", handle.core.context.capability_registry.descriptors)
        self.assertIn("intro_module_web_search_show", handle.core.context.capability_registry.descriptors)
        self.assertIn("intro_module_web_fetch_show", handle.core.context.capability_registry.descriptors)
        self.assertTrue(
            any(name.startswith("op_web_search_mgmt_set_config") for name in handle.core.context.capability_registry.descriptors)
        )
        self.assertTrue(
            any(name.startswith("op_web_fetch_mgmt_set_config") for name in handle.core.context.capability_registry.descriptors)
        )
        self.assertIn("op_web_search_query", tool_names)
        self.assertIn("op_web_fetch_read", tool_names)

    def test_plugin_host_rescan_discovers_third_party_bundle_but_does_not_import_it(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )
        plugin_root = self.registration.runtime.runtime_root / "plugins" / "demo_plugin"
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
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
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
                CapabilityCall(name="op_plugin_mgmt_rescan_and_attach_new_first_party")
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

    def test_plugins_module_publishes_management_capabilities_and_can_detach_first_party_plugin(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        self.assertIn("intro_module_plugins_show", handle.core.context.capability_registry.descriptors)
        self.assertIn("op_plugin_mgmt_detach", handle.core.context.capability_registry.descriptors)
        self.assertIn("intro_provider_l3_show::sqlite_vec_l3", handle.core.context.capability_registry.descriptors)

        detached = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="op_plugin_mgmt_detach", args={"plugin_id": "sqlite_vec_l3"})
        )

        self.assertEqual(detached.status, "ok")
        self.assertNotIn("intro_provider_l3_show::sqlite_vec_l3", handle.core.context.capability_registry.descriptors)

    def test_plugin_attach_detach_lifecycle_works_end_to_end(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )
        runtime = handle.core.context.execution_runtime
        cap_registry = handle.core.context.capability_registry
        plugin_host = handle.plugin_host

        # Verify sqlite_vec_l3 starts attached with capabilities
        l3_caps_before = [name for name in cap_registry.descriptors if "sqlite_vec_l3" in name]
        self.assertTrue(len(l3_caps_before) > 0, "L3 plugin should have capabilities on boot")
        self.assertIsNotNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # DETACH
        detached = runtime.execute(
            CapabilityCall(name="op_plugin_mgmt_detach", args={"plugin_id": "sqlite_vec_l3"})
        )
        self.assertEqual(detached.status, "ok")

        # Capabilities withdrawn
        l3_caps_after_detach = [name for name in cap_registry.descriptors if "sqlite_vec_l3" in name]
        self.assertEqual(len(l3_caps_after_detach), 0, "All L3 capabilities should be withdrawn after detach")

        # Provider ref removed
        self.assertIsNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # Record shows detached
        records = plugin_host.list_plugins()
        l3_record = next(r for r in records if r["plugin_id"] == "sqlite_vec_l3")
        self.assertFalse(l3_record["attached"])

        # RE-ATTACH
        attached = runtime.execute(
            CapabilityCall(name="op_plugin_mgmt_attach", args={"plugin_id": "sqlite_vec_l3"})
        )
        self.assertEqual(attached.status, "ok")

        # Capabilities restored
        l3_caps_after_reattach = [name for name in cap_registry.descriptors if "sqlite_vec_l3" in name]
        self.assertEqual(len(l3_caps_after_reattach), len(l3_caps_before), "All L3 capabilities should be restored after re-attach")

        # Provider ref restored
        self.assertIsNotNone(runtime.l3_plugin_registry.get("sqlite_vec_l3"))

        # Record shows attached
        records2 = plugin_host.list_plugins()
        l3_record2 = next(r for r in records2 if r["plugin_id"] == "sqlite_vec_l3")
        self.assertTrue(l3_record2["attached"])

    def test_memory_l3_regression_build_pack_uses_builtin_sqlite_vec_provider(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        commit = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_commit_write",
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
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        committed = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_commit_write",
                args={
                    "target_id": "sqlite_vec_l3",
                    "kind": "case",
                    "scope": "task",
                    "task_id": "task-1",
                    "title": "Recover worker",
                    "summary": "Recovered the worker after memory pressure crash.",
                    "search_text": "Worker crashed under memory pressure. Restarted the worker and reduced concurrency. Queue drain recovered and latency normalized.",
                    "situation_text": "Worker crashed under memory pressure",
                    "task_text": "Stabilize the worker",
                    "action_text": "Restarted it and reduced concurrency",
                    "result_text": "Latency normalized",
                    "topics": ["worker", "stability"],
                },
            )
        )
        document_id = committed.structured["document_id"]

        recalled = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_recall_query",
                args={
                    "target_id": "sqlite_vec_l3",
                    "queries": ["worker memory pressure stabilize"],
                    "limit": 4,
                },
            )
        )
        recalled_origin = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_recall_query",
                args={
                    "target_id": "sqlite_vec_l3",
                    "queries": ["worker memory pressure stabilize"],
                    "limit": 4,
                    "view": "origin",
                },
            )
        )
        corrected = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_l3_correct_patch",
                args={
                    "target_id": "sqlite_vec_l3",
                    "document_id": document_id,
                    "summary": "Recovered the worker after memory pressure.",
                    "topics": ["worker", "recovery"],
                },
            )
        )
        inventory = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="intro_provider_l3_inventory",
                args={"target_id": "sqlite_vec_l3"},
            )
        )

        self.assertEqual(committed.status, "ok")
        self.assertEqual(recalled.status, "ok")
        self.assertEqual(recalled_origin.status, "ok")
        self.assertEqual(corrected.status, "ok")
        self.assertEqual(recalled.structured["hit_count"], 1)
        self.assertEqual(recalled.structured["hits_preview"][0]["document_id"], document_id)
        self.assertEqual(recalled.structured["view"], "summary")
        self.assertNotIn("hits", recalled.structured)
        self.assertNotIn("projected_entries", recalled.structured)
        self.assertNotIn("projected_entries", recalled.llm_text)
        self.assertNotIn("Restarted the worker and reduced concurrency.", recalled.llm_text)
        self.assertIn("Recovered the worker after memory pressure crash.", recalled.llm_text)
        self.assertEqual(recalled_origin.structured["view"], "origin")
        self.assertEqual(recalled_origin.structured["hit_count"], 1)
        self.assertNotIn("hits", recalled_origin.structured)
        self.assertNotIn("projected_entries", recalled_origin.structured)
        self.assertIn("Restarted the worker and reduced concurrency.", recalled_origin.llm_text)
        self.assertNotIn("projected_entries", recalled_origin.llm_text)
        self.assertEqual(inventory.status, "ok")
        self.assertEqual(inventory.structured["provider_id"], "sqlite_vec_l3")
        self.assertIn(document_id, handle.memory_service.l2_store.items)
        self.assertIn(document_id, handle.memory_service.l2_store.heat_registry)
        self.assertEqual(handle.memory_service.l2_store.items[document_id].summary, "Recovered the worker after memory pressure.")

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
                title="Recover worker",
                summary="Recovered the worker after memory pressure crash.",
                search_text="Worker crashed under memory pressure. Restarted the worker and reduced concurrency. Queue drain recovered and latency normalized.",
                situation_text="Worker crashed under memory pressure",
                task_text="Stabilize the worker",
                action_text="Restarted it and reduced concurrency",
                result_text="Latency normalized",
                topics=["worker", "stability"],
            )
        )

        recall = provider.recall(MemoryQuery(queries=["worker memory pressure stabilize"], limit=4))

        self.assertEqual(recall.hits[0]["document_id"], result.document_id)
        self.assertEqual(recall.metadata["retrieval_mode"], "lexical")
        self.assertTrue(recall.metadata["degraded"])
        self.assertGreaterEqual(recall.metadata["candidate_sources"]["fts_word"], 1)

    def test_sqlite_vec_l3_recall_supports_cjk_trigram_and_short_like_fallback(self) -> None:
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

        trigram_hit = provider.recall(MemoryQuery(queries=["简洁中文回复"], limit=4))
        short_hit = provider.recall(MemoryQuery(queries=["简洁"], limit=4))

        self.assertEqual(trigram_hit.hits[0]["title"], "回复风格")
        self.assertGreaterEqual(trigram_hit.metadata["candidate_sources"]["fts_trigram"], 1)
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
            {"fts_word": 2, "fts_trigram": 0, "like": 0},
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
                title="Restart flaky worker",
                summary="Restarted flaky worker after memory pressure crash.",
                search_text="Worker crashed after high memory pressure. Task was to stabilize the background worker. Restarted the worker and reduced concurrency. Queue drain recovered and latency normalized.",
                situation_text="Worker crashed after high memory pressure",
                task_text="Stabilize the background worker",
                action_text="Restarted the worker and reduced concurrency",
                result_text="Queue drain recovered and latency normalized",
                topics=["worker", "stability"],
            )
        )
        provider.refresh_indexes(limit=8)

        st_hit = provider.recall(MemoryQuery(queries=["worker high memory pressure stabilize"], limit=4))
        ar_only = provider.recall(MemoryQuery(queries=["latency normalized"], limit=4))

        self.assertEqual(st_hit.hits[0]["document_id"], result.document_id)
        self.assertIn("Action: Restarted the worker", st_hit.hits[0]["rendered"])
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

    def test_litellm_invoker_does_not_map_think_level_to_reasoning_effort_for_openai_chat(self) -> None:
        endpoint = LLMEndpointRepository().upsert(
            endpoint_id="deepseek",
            provider="deepseek",
            model_id="deepseek/deepseek-chat",
            api_mode="openai_chat",
            base_url="https://api.deepseek.com/v1",
            credential_ref="deepseek-prod",
            enabled=True,
        )
        invoker = LiteLLMEndpointInvoker(
            credentials=LiteLLMCredentialResolver(secret_store=InMemorySecretStore())
        )

        kwargs = invoker._build_completion_kwargs(
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
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(repository=endpoint_repository),
            settings_repository=settings_repository,
            endpoint_invoker=invoker,
            endpoint_retry_attempts=2,
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
        self.assertEqual(runtime.last_request.metadata["think_level"], DEFAULT_THINK_LEVEL)
        self.assertEqual(runtime.last_request.metadata["endpoint_id"], "fallback")

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

    def test_litellm_invoker_stream_normalizes_reasoning_only_chunks(self) -> None:
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

        class FakeLiteLLM:
            @staticmethod
            def completion(**kwargs):
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

        invoker = LiteLLMEndpointInvoker(
            credentials=LiteLLMCredentialResolver(secret_store=InMemorySecretStore())
        )
        request = CanonicalLLMRequest(
            messages=[{"role": "user", "content": "Reply with exactly: pong"}],
            max_output_tokens=32,
        )

        with patch.dict(sys.modules, {"litellm": FakeLiteLLM}):
            events = list(invoker.invoke_stream(endpoint, request))

        self.assertEqual(
            [event.event_kind for event in events],
            [LLMStreamEventKind.REASONING_DELTA, LLMStreamEventKind.REASONING_DELTA, LLMStreamEventKind.DONE],
        )
        self.assertEqual("".join(event.reasoning_text for event in events), "thinkingstill thinking")
        self.assertEqual(events[-1].finish_reason, "length")

    def test_litellm_response_parser_preserves_reasoning_text(self) -> None:
        invoker = LiteLLMEndpointInvoker(
            credentials=LiteLLMCredentialResolver(secret_store=InMemorySecretStore())
        )

        class FakeResponse:
            def to_dict(self):
                return {
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

        outcome = invoker._parse_litellm_response(FakeResponse())

        self.assertEqual(outcome.text, "pong")
        self.assertEqual(outcome.reasoning_text, "thinking")

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
        self.assertIn("backend offline", outcome.text)

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

    def test_supervisor_provisions_stub_runtime_before_bootstrap_composition(self) -> None:
        provisioned = self.supervisor.provision_stub_runtime(self.runtime_root / "stub_runtime")
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=provisioned.registration,
            database=provisioned.database,
        )
        handle.database.close()

        self.assertEqual(handle.registration.runtime.db_path, handle.database.db_path)
        self.assertEqual(handle.supervisor.registrations[-1], handle.registration)
        self.assertIsNotNone(handle.core.context.module_registry.get("core"))
        self.assertIsNotNone(handle.core.context.module_registry.get("channel"))
        self.assertIsNotNone(handle.core.context.module_registry.get("llm"))
        self.assertIsNotNone(handle.core.context.module_registry.get("memory"))
        self.assertIsNotNone(handle.core.context.module_registry.get("identity"))
        self.assertIsNotNone(handle.core.context.execution_runtime.capabilities.get("intro_module_llm_list"))

    def test_compose_runtime_registers_seeded_socket_endpoint(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        endpoint = handle.channel_runtime.get_endpoint("socket_default")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.endpoint.channel_kind, "socket")
        self.assertEqual(Path(endpoint.endpoint.binding_key), self.registration.runtime.runtime_root / "pal.sock")

    def test_compose_runtime_registers_telegram_endpoint_via_factory_registry(self) -> None:
        self.supervisor.seed_defaults(self.registration)
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
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        endpoint = handle.channel_runtime.get_endpoint("telegram_main")
        self.assertIsNotNone(endpoint)
        self.assertIsInstance(endpoint, TelegramChannelEndpoint)
        self.assertEqual(endpoint.endpoint.binding_key, "chat:123")

    def test_llm_capabilities_are_read_only_and_do_not_expose_credentials(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        llm_list = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="intro_module_llm_list")
        )
        llm_active = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="intro_module_llm_active")
        )
        llm_think_level = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="intro_module_llm_think_level")
        )
        llm_show = handle.core.context.execution_runtime.execute(
            CapabilityCall(name="intro_endpoint_llm_show", args={"target_id": "stub_llm_default"})
        )

        self.assertEqual(llm_list.status, "ok")
        self.assertEqual(llm_active.status, "ok")
        self.assertEqual(llm_think_level.status, "ok")
        self.assertEqual(llm_show.status, "ok")
        self.assertNotIn("credential_ref", llm_show.structured)
        self.assertNotIn("base_url", llm_show.structured)
        self.assertEqual(llm_think_level.structured["effective_think_level"], DEFAULT_THINK_LEVEL)

    def test_compose_runtime_consumes_supervisor_owned_database(self) -> None:
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )

        self.assertEqual(handle.database.db_path, self.registration.runtime.db_path)
        self.assertEqual(handle.registration.display_name, "PalV2 Test")

    def test_web_search_capability_falls_back_and_auth_material_is_sanitized(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )
        service = handle.core.context.module_registry.require("web_search").ports["web_search"]

        auth_result = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_web_search_mgmt_set_auth_material",
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
                name="op_web_search_query",
                args={"query": "pal runtime docs", "limit": 3},
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["fallback_used"])
        self.assertEqual(result.structured["configured_provider_id"], "brave_search_default")
        self.assertEqual(result.structured["effective_provider_id"], "duckduckgo_search_default")
        self.assertEqual(result.structured["items"][0]["title"], "Pal runtime docs")

    def test_web_fetch_health_does_not_start_browser_service_and_disable_stops_manager(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )
        service = handle.core.context.module_registry.require("web_fetch").ports["web_fetch"]

        health = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="intro_provider_web_fetch_health",
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
                name="op_web_fetch_mgmt_disable",
                args={"target_id": "playwright_fetch_default"},
            )
        )

        self.assertEqual(disabled.status, "ok")
        self.assertEqual(fake_manager.stop_calls, 1)

    def test_web_fetch_capability_falls_back_to_plain_http_and_runtime_stop_runs_shutdown_hook(self) -> None:
        self.supervisor.seed_defaults(self.registration)
        handle = compose_runtime(
            supervisor=self.supervisor,
            registration=self.registration,
            database=self.database,
        )
        service = handle.core.context.module_registry.require("web_fetch").ports["web_fetch"]

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
                return {
                    "requested_url": request.url,
                    "final_url": request.url,
                    "title": "Example Domain",
                    "text": "Example body text",
                }

        service.providers = {
            "playwright_fetch": RaisingFetchProvider(),
            "plain_http_fetch": StaticFetchProvider(),
        }

        result = handle.core.context.execution_runtime.execute(
            CapabilityCall(
                name="op_web_fetch_read",
                args={"url": "https://example.com"},
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["fallback_used"])
        self.assertEqual(result.structured["configured_provider_id"], "playwright_fetch_default")
        self.assertEqual(result.structured["effective_provider_id"], "plain_http_fetch_default")
        self.assertEqual(result.structured["fetch_mode"], "http")

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


class PalV2SocketEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
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
            await asyncio.sleep(0.05)

            envelopes = self.endpoint.poll()
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
            await asyncio.sleep(0.05)
            envelopes = self.endpoint.poll()
            self.assertEqual(len(envelopes), 1)
            envelope = envelopes[0]
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)

        self.endpoint.queue_reply("pong", response_handle=envelope.response_handle)
        emitted = self.endpoint.flush_outbox()

        self.assertEqual([event.event_kind for event in emitted], ["reply.failed"])
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

    async def set_message_reaction(self, **kwargs):
        self.actions.append(("reaction", dict(kwargs)))

    async def send_chat_action(self, **kwargs):
        self.actions.append(("typing", dict(kwargs)))

    async def send_message(self, **kwargs):
        self.actions.append(("message", dict(kwargs)))

    async def get_file(self, file_id: str):
        return self.files[file_id]


class _FakeTelegramUpdater:
    def __init__(self) -> None:
        self.running = False

    async def start_polling(self, **kwargs):
        _ = kwargs
        self.running = True

    async def stop(self):
        self.running = False


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

    async def test_telegram_endpoint_accepts_matching_binding_and_queues_statuses(self) -> None:
        update = _FakeTelegramUpdate(
            update_id=2,
            message=_FakeTelegramMessage(chat_id=100, user_id=42, message_id=10, text="hello"),
        )

        await self.endpoint._on_update(update, None)

        envelopes = self.endpoint.poll()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].event.payload["text"], "hello")
        self.endpoint.flush_status_outbox()
        await asyncio.sleep(0.05)
        self.assertTrue(any(kind == "reaction" for kind, _ in self.fake_bot.actions))
        self.assertTrue(any(kind == "typing" for kind, _ in self.fake_bot.actions))

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
        self.assertTrue(Path(attachment["local_cached_path"]).exists())

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
